#!/usr/bin/env python3
"""Coolify error watchdog: fetch app logs, extract & fingerprint errors.

Standalone by design — stdlib only, no repo imports — so any project on the
same Coolify instance can reuse it unchanged.

Environment variables:
    COOLIFY_API_URL      Base URL of the Coolify instance (e.g. https://apps.example.com)
    COOLIFY_API_TOKEN    Bearer token (Coolify UI -> Keys & Tokens -> API tokens)
    COOLIFY_APPS         JSON list: [{"name": "backend", "uuid": "abc123"}, ...]
    LOG_LINES            Lines to fetch per app from the end of the log (default 3000)
    SINCE_HOURS          Only report events younger than this (default 26)
    KNOWN_FINGERPRINTS   Comma-separated fingerprints that already have a PR/branch
    MAX_NEW_ERRORS       Cap on errors written to errors.json (default 3)
    IGNORE_FILE          Path to fingerprint ignore list (default: ignore.txt next to this file)
    OUTPUT_FILE          Where to write the errors JSON (default: errors.json in cwd)

Outputs (for GitHub Actions):
    $GITHUB_OUTPUT       has_new_errors=true|false, new_error_count=N
    $GITHUB_STEP_SUMMARY human-readable markdown summary
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Docker/Coolify may prefix each line with an ISO timestamp (docker logs -t style).
DOCKER_TS_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s"
)
# Production format from backend/logging_config.py: "%Y-%m-%d %H:%M:%S - LEVEL - name - msg"
APP_TS_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2})")
# Gunicorn: "[2026-07-01 22:14:03 +0000] [1] [ERROR] ..."
GUNICORN_TS_RE = re.compile(
    r"^\[(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}) [+-]\d{4}\]"
)

LEVEL_RE = re.compile(r"\s-\s(ERROR|CRITICAL)\s-\s")
GUNICORN_LEVEL_RE = re.compile(r"\[(ERROR|CRITICAL)\]")
HTTP_5XX_RE = re.compile(r'HTTP/\d(?:\.\d)?"?\s+5\d{2}\b')
TRACEBACK_START = "Traceback (most recent call last):"
FRAME_RE = re.compile(r'File "(?P<path>[^"]+)", line \d+, in (?P<func>\S+)')

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
DIGITS_RE = re.compile(r"\d+")

MAX_EXCERPT_CHARS = 2000


def strip_prefix(line: str) -> tuple[str, datetime | None]:
    """Remove a leading docker timestamp; return the clean line and the best timestamp.

    Falls back to the app/gunicorn timestamp embedded in the line itself.
    Naive timestamps are assumed UTC (both the containers and docker log in UTC).
    """
    ts = None
    m = DOCKER_TS_RE.match(line)
    if m:
        ts = _to_dt(m.group("date"), m.group("time"))
        line = line[m.end():]
    for pattern in (APP_TS_RE, GUNICORN_TS_RE):
        m = pattern.match(line)
        if m:
            embedded = _to_dt(m.group("date"), m.group("time"))
            ts = ts or embedded
            break
    return line.rstrip("\r"), ts


def _to_dt(date: str, time: str) -> datetime | None:
    try:
        return datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _consume_traceback(lines: list[tuple[str, datetime | None]], i: int) -> tuple[list[str], int]:
    """Collect a traceback block starting at index i (which contains TRACEBACK_START).

    The block is the 'Traceback' header, the indented frames, and the final
    non-indented exception line (inclusive).
    """
    block = [lines[i][0]]
    i += 1
    while i < len(lines):
        clean = lines[i][0]
        block.append(clean)
        i += 1
        if not clean.startswith((" ", "\t")) and TRACEBACK_START not in clean:
            break  # exception line ends the block
    return block, i


def extract_events(text: str) -> list[dict]:
    """Extract error events from raw log text.

    Event kinds: 'traceback', 'error_log' (ERROR/CRITICAL line, with any
    directly-following traceback attached), 'http_5xx'.
    """
    lines = [strip_prefix(raw) for raw in text.splitlines() if raw.strip()]
    events: list[dict] = []
    i = 0
    while i < len(lines):
        clean, ts = lines[i]
        if TRACEBACK_START in clean:
            block, i = _consume_traceback(lines, i)
            events.append({"kind": "traceback", "lines": block, "ts": ts})
            continue
        if LEVEL_RE.search(clean) or GUNICORN_LEVEL_RE.search(clean):
            block = [clean]
            i += 1
            if i < len(lines) and TRACEBACK_START in lines[i][0]:
                tb, i = _consume_traceback(lines, i)
                block.extend(tb)
            events.append({"kind": "error_log", "lines": block, "ts": ts})
            continue
        if HTTP_5XX_RE.search(clean):
            events.append({"kind": "http_5xx", "lines": [clean], "ts": ts})
            i += 1
            continue
        i += 1
    return events


def _normalize(text: str) -> str:
    text = UUID_RE.sub("<uuid>", text)
    text = HEX_ADDR_RE.sub("<addr>", text)
    text = DIGITS_RE.sub("#", text)
    return " ".join(text.split())


def fingerprint(app: str, event: dict) -> str:
    """Stable short id for an error class: same bug on different nights,
    different line numbers or request ids => same fingerprint."""
    lines = event["lines"]
    if any(TRACEBACK_START in ln for ln in lines):
        frames = [m for ln in lines if (m := FRAME_RE.search(ln))]
        last_frame = ""
        if frames:
            m = frames[-1]
            # Keep only the file's basename so container vs local paths agree.
            last_frame = f'{m.group("path").rsplit("/", 1)[-1]}:{m.group("func")}'
        exception_line = lines[-1]
        signature = f"{last_frame}|{_normalize(exception_line)}"
    else:
        signature = _normalize(lines[0])
    digest = hashlib.sha1(f"{app}|{event['kind']}|{signature}".encode()).hexdigest()
    return digest[:10]


def group_errors(events: list[dict], app: str) -> list[dict]:
    """Group events by fingerprint into error records, most frequent first."""
    groups: dict[str, dict] = {}
    for event in events:
        fp = fingerprint(app, event)
        record = groups.get(fp)
        if record is None:
            groups[fp] = record = {
                "fingerprint": fp,
                "app": app,
                "kind": event["kind"],
                "count": 0,
                "first_seen": None,
                "last_seen": None,
                "excerpt": "\n".join(event["lines"])[:MAX_EXCERPT_CHARS],
            }
        record["count"] += 1
        ts = event["ts"]
        if ts is not None:
            iso = ts.isoformat()
            if record["first_seen"] is None or iso < record["first_seen"]:
                record["first_seen"] = iso
            if record["last_seen"] is None or iso > record["last_seen"]:
                record["last_seen"] = iso
    return sorted(groups.values(), key=lambda r: r["count"], reverse=True)


def filter_recent(events: list[dict], now: datetime, since_hours: float) -> list[dict]:
    """Drop events older than the window. Events without a parseable timestamp
    are kept — better a duplicate report (deduped by fingerprint) than a miss."""
    cutoff = now - timedelta(hours=since_hours)
    return [e for e in events if e["ts"] is None or e["ts"] >= cutoff]


def load_ignored(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {
            line.strip()
            for line in fh
            if line.strip() and not line.strip().startswith("#")
        }


def fetch_app_logs(api_url: str, token: str, uuid: str, lines: int) -> str:
    url = f"{api_url.rstrip('/')}/api/v1/applications/{uuid}/logs?lines={lines}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    if isinstance(payload, dict):
        return str(payload.get("logs", ""))
    return body


def _append(path_env: str, content: str) -> None:
    path = os.getenv(path_env)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(content)


def render_summary(new_errors: list[dict], skipped: int, dropped: int, failures: list[str]) -> str:
    parts = ["## Coolify error watchdog\n"]
    if failures:
        parts.append("**⚠️ Log fetch failed for:** " + ", ".join(failures) + "\n")
    if not new_errors:
        parts.append("No new errors found. 🎉\n")
    else:
        parts.append(f"**{len(new_errors)} new error(s) found:**\n")
        for err in new_errors:
            parts.append(
                f"- `{err['fingerprint']}` ({err['app']}, {err['kind']}, "
                f"seen {err['count']}x, last {err['last_seen'] or 'unknown'})"
            )
        parts.append("")
    if skipped:
        parts.append(f"_Skipped {skipped} known/ignored error(s)._")
    if dropped:
        parts.append(f"_Dropped {dropped} additional new error(s) over the nightly cap._")
    return "\n".join(parts) + "\n"


def main() -> int:
    api_url = os.getenv("COOLIFY_API_URL", "")
    token = os.getenv("COOLIFY_API_TOKEN", "")
    apps_raw = os.getenv("COOLIFY_APPS", "")
    if not api_url or not token or not apps_raw:
        print("ERROR: COOLIFY_API_URL, COOLIFY_API_TOKEN and COOLIFY_APPS are required")
        return 1
    try:
        apps = json.loads(apps_raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: COOLIFY_APPS is not valid JSON: {exc}")
        return 1

    log_lines = int(os.getenv("LOG_LINES", "3000"))
    since_hours = float(os.getenv("SINCE_HOURS", "26"))
    max_new = int(os.getenv("MAX_NEW_ERRORS", "3"))
    known = {fp.strip() for fp in os.getenv("KNOWN_FINGERPRINTS", "").split(",") if fp.strip()}
    ignore_file = os.getenv(
        "IGNORE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ignore.txt")
    )
    output_file = os.getenv("OUTPUT_FILE", "errors.json")
    ignored = load_ignored(ignore_file)
    now = datetime.now(timezone.utc)

    all_groups: list[dict] = []
    failures: list[str] = []
    for app in apps:
        name, uuid = app.get("name", "app"), app.get("uuid", "")
        try:
            text = fetch_app_logs(api_url, token, uuid, log_lines)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"WARNING: failed to fetch logs for {name} ({uuid}): {exc}")
            failures.append(name)
            continue
        events = filter_recent(extract_events(text), now, since_hours)
        groups = group_errors(events, name)
        print(f"{name}: {len(events)} error event(s), {len(groups)} distinct")
        all_groups.extend(groups)

    if failures and len(failures) == len(apps):
        print("ERROR: log fetch failed for every app — check COOLIFY_API_URL/token/uuids")
        return 1

    excluded = known | ignored
    new_errors = [g for g in all_groups if g["fingerprint"] not in excluded]
    skipped = len(all_groups) - len(new_errors)
    new_errors.sort(key=lambda r: r["count"], reverse=True)
    dropped = max(0, len(new_errors) - max_new)
    new_errors = new_errors[:max_new]

    with open(output_file, "w", encoding="utf-8") as fh:
        json.dump(new_errors, fh, indent=2)

    _append(
        "GITHUB_OUTPUT",
        f"has_new_errors={'true' if new_errors else 'false'}\n"
        f"new_error_count={len(new_errors)}\n",
    )
    _append("GITHUB_STEP_SUMMARY", render_summary(new_errors, skipped, dropped, failures))
    print(
        f"{len(new_errors)} new, {skipped} known/ignored, {dropped} over cap "
        f"-> {output_file}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
