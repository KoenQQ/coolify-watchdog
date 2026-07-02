"""Unit tests for the Coolify error watchdog parser.

Run with: pytest scripts/coolify_watchdog/
Fully isolated — no network, no repo dependencies.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_errors as fe  # noqa: E402

NOW = datetime(2026, 7, 2, 6, 0, 0, tzinfo=timezone.utc)

TRACEBACK_A = """\
2026-07-02 03:14:03 - ERROR - routers.matching - Matching failed
Traceback (most recent call last):
  File "/app/routers/matching.py", line 142, in match_profiles
    result = matcher.run(query, org_id)
  File "/app/enhanced_profile_matcher.py", line 88, in run
    return self._score(profiles[0])
IndexError: list index out of range
"""

# Same bug, different night: line numbers and request-specific values differ.
TRACEBACK_A_SHIFTED = """\
2026-07-02 04:22:17 - ERROR - routers.matching - Matching failed
Traceback (most recent call last):
  File "/app/routers/matching.py", line 150, in match_profiles
    result = matcher.run(query, org_id)
  File "/app/enhanced_profile_matcher.py", line 91, in run
    return self._score(profiles[0])
IndexError: list index out of range
"""

TRACEBACK_B = """\
Traceback (most recent call last):
  File "/app/database.py", line 55, in get_connection_context
    conn = self.pool.getconn()
psycopg2.pool.PoolError: connection pool exhausted
"""


def _fp(text: str, app: str = "backend") -> str:
    events = fe.extract_events(text)
    assert events, "expected at least one event"
    return fe.fingerprint(app, events[0])


def test_traceback_extracted_with_full_block():
    events = fe.extract_events(TRACEBACK_A)
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "error_log"  # ERROR line with traceback attached
    assert event["lines"][0].endswith("Matching failed")
    assert event["lines"][-1] == "IndexError: list index out of range"
    assert event["ts"] == datetime(2026, 7, 2, 3, 14, 3, tzinfo=timezone.utc)


def test_bare_traceback_without_error_line():
    events = fe.extract_events(TRACEBACK_B)
    assert len(events) == 1
    assert events[0]["kind"] == "traceback"
    assert events[0]["lines"][-1].startswith("psycopg2.pool.PoolError")


def test_fingerprint_stable_across_line_numbers():
    assert _fp(TRACEBACK_A) == _fp(TRACEBACK_A_SHIFTED)


def test_fingerprint_differs_for_different_errors():
    assert _fp(TRACEBACK_A) != _fp(TRACEBACK_B)


def test_fingerprint_differs_per_app():
    assert _fp(TRACEBACK_B, app="backend") != _fp(TRACEBACK_B, app="frontend")


def test_fingerprint_normalizes_uuids_and_ids():
    line1 = "2026-07-02 03:00:00 - ERROR - api - profile 0f8fad5b-d9cb-469f-a165-70867728950e not found (req 4711)"
    line2 = "2026-07-02 05:30:00 - ERROR - api - profile 7c9e6679-7425-40de-944b-e07fc1f90ae7 not found (req 9218)"
    assert _fp(line1) == _fp(line2)


def test_prod_and_gunicorn_error_lines_detected():
    text = (
        "2026-07-02 03:00:00 - ERROR - database - query timeout\n"
        "[2026-07-02 03:01:00 +0000] [1] [ERROR] Worker (pid:42) was sent SIGKILL\n"
        "2026-07-02 03:02:00 - INFO - api - all good\n"
    )
    events = fe.extract_events(text)
    assert len(events) == 2
    assert all(e["kind"] == "error_log" for e in events)


def test_http_5xx_detected_but_2xx_ignored():
    text = (
        '10.0.0.1 - - "POST /api/profiles/match HTTP/1.1" 500 42\n'
        '10.0.0.1 - - "GET /api/profiles HTTP/1.1" 200 1234\n'
    )
    events = fe.extract_events(text)
    assert len(events) == 1
    assert events[0]["kind"] == "http_5xx"


def test_docker_timestamp_prefix_stripped():
    text = (
        "2026-07-02T03:14:03.123456789Z 2026-07-02 03:14:03 - ERROR - api - boom\n"
    )
    events = fe.extract_events(text)
    assert len(events) == 1
    assert events[0]["lines"][0].startswith("2026-07-02 03:14:03 - ERROR")
    assert events[0]["ts"] == datetime(2026, 7, 2, 3, 14, 3, tzinfo=timezone.utc)


def test_docker_prefix_used_when_line_has_no_timestamp():
    text = "2026-07-02T03:14:03Z Traceback (most recent call last):\n" \
           '2026-07-02T03:14:03Z   File "/app/x.py", line 1, in f\n' \
           "2026-07-02T03:14:03Z ValueError: nope\n"
    events = fe.extract_events(text)
    assert len(events) == 1
    assert events[0]["ts"] == datetime(2026, 7, 2, 3, 14, 3, tzinfo=timezone.utc)


def test_old_events_filtered_but_untimestamped_kept():
    old = {"kind": "error_log", "lines": ["x"], "ts": NOW.replace(day=1, hour=1)}
    recent = {"kind": "error_log", "lines": ["y"], "ts": NOW}
    unknown = {"kind": "error_log", "lines": ["z"], "ts": None}
    kept = fe.filter_recent([old, recent, unknown], NOW, since_hours=26)
    assert kept == [recent, unknown]


def test_group_errors_counts_and_timestamps():
    events = fe.extract_events(TRACEBACK_A + TRACEBACK_A_SHIFTED + TRACEBACK_B)
    groups = fe.group_errors(events, "backend")
    assert len(groups) == 2
    top = groups[0]
    assert top["count"] == 2
    assert top["first_seen"] == "2026-07-02T03:14:03+00:00"
    assert top["last_seen"] == "2026-07-02T04:22:17+00:00"
    assert "IndexError" in top["excerpt"]


def test_no_errors_in_clean_logs():
    text = (
        "2026-07-02 03:00:00 - INFO - api - startup complete\n"
        '10.0.0.1 - - "GET /api/health HTTP/1.1" 200 12\n'
        "2026-07-02 03:00:01 - WARNING - httpx - retrying\n"
    )
    assert fe.extract_events(text) == []


def test_ignore_file_parsing(tmp_path):
    path = tmp_path / "ignore.txt"
    path.write_text("# known noisy startup error\nabc123def4\n\n  fed321cba9  \n")
    assert fe.load_ignored(str(path)) == {"abc123def4", "fed321cba9"}


def test_ignore_file_missing(tmp_path):
    assert fe.load_ignored(str(tmp_path / "nope.txt")) == set()


def test_end_to_end_dedupe_flow():
    """Known fingerprints (existing PRs/branches) are excluded from new errors."""
    events = fe.extract_events(TRACEBACK_A + TRACEBACK_B)
    groups = fe.group_errors(events, "backend")
    known = {groups[0]["fingerprint"]}
    fresh = [g for g in groups if g["fingerprint"] not in known]
    assert len(fresh) == 1
    assert fresh[0]["fingerprint"] == groups[1]["fingerprint"]
