# Coolify Error Watchdog

Nightly automation for every project running on the Coolify instance: reads
production container logs via the Coolify API, detects **new** errors, and has
Claude open a **draft PR** in the affected project with a root-cause analysis
and a proposed fix — assigned to the repo owner, so it arrives as a GitHub
mobile push notification in the morning.

```
02:30 UTC nightly (GitHub Actions, in each project repo)
 ├─ fetch_errors.py      pull logs via Coolify API, extract + fingerprint errors
 ├─ dedupe               skip fingerprints that already have a watchdog PR/branch
 └─ claude-code-action   for new errors (max 3/night): root-cause analysis,
                         branch watchdog/<date>-<fingerprint>, minimal fix,
                         draft PR labeled `log-watchdog`
```

This repo is the **engine**: the reusable workflow
(`.github/workflows/coolify-watchdog.yml`, `workflow_call`-only), the log
parser (`scripts/coolify_watchdog/fetch_errors.py`, stdlib-only), the caller
template, and the setup skill. Projects contain only a ~40-line caller
workflow — fixes and improvements here reach every project automatically.

No new errors → a project's run ends in ~1 minute and Claude is never invoked
(zero Claude cost on quiet nights).

## Adding the watchdog to a project

Either run the `setup-coolify-watchdog` Claude Code skill (copy
`skills/setup-coolify-watchdog/` to `~/.claude/skills/` once, then run
`/setup-coolify-watchdog` inside the project), or manually:

1. Copy `templates/caller-workflow.yml` into the project as
   `.github/workflows/coolify-watchdog.yml`.
2. Configure the project repo (Settings → Secrets and variables → Actions):

   | Type     | Name                      | Value                                                                 |
   |----------|---------------------------|-----------------------------------------------------------------------|
   | Secret   | `COOLIFY_API_TOKEN`       | Coolify UI → Keys & Tokens → API tokens → new token                    |
   | Secret   | `ANTHROPIC_API_KEY`       | Anthropic Console key (pay-per-use API billing) — **or** the one below |
   | Secret   | `CLAUDE_CODE_OAUTH_TOKEN` | From `claude setup-token`; draws from the Claude Pro/Max subscription  |
   | Secret   | `ENGINE_REPO_TOKEN`       | Fine-grained PAT with read access to this repo — only if it is private |
   | Variable | `COOLIFY_API_URL`         | e.g. `https://apps.pompadour.ventures`                                 |
   | Variable | `COOLIFY_APPS`            | `[{"name":"backend","uuid":"<coolify-app-uuid>"}]`                     |

   The app UUID is in the Coolify UI URL when the application is open (or via
   `GET /api/v1/applications`).

3. **First run:** project's Actions tab → *Coolify Error Watchdog* → Run
   workflow → `dry_run: true`. The job summary lists what was found without
   invoking Claude or opening PRs — this validates the token and UUIDs.

### Repo visibility

- **Public engine repo (simplest):** no `ENGINE_REPO_TOKEN`, no access setup.
  The engine contains no secrets or project-specific data.
- **Private engine repo:** enable Settings → Actions → General → Access →
  *"Accessible from repositories owned by the user"* here, and add
  `ENGINE_REPO_TOKEN` to each project.

## How new errors become PRs

- Every error event (Python traceback, `ERROR`/`CRITICAL` log line, HTTP 5xx)
  is **fingerprinted**: a 10-char hash of the normalized signature (exception
  type + raising function, with line numbers / UUIDs / ids stripped). The same
  bug on different nights yields the same fingerprint.
- A fingerprint is **skipped** if any `watchdog/*` branch or `log-watchdog` PR
  (open *or* closed) in the project already references it — so rejecting a
  proposed fix doesn't cause a re-spam every night.
  - **Retry an error:** delete its PR *and* `watchdog/...` branch.
  - **Silence an error permanently:** add its fingerprint to the project's
    `scripts/coolify_watchdog/ignore.txt`.
- At most **3 new errors per night** per project are analyzed (cost/noise
  cap); the rest are listed in the job summary and picked up the next night.
- Each PR body contains: *What happened* (log excerpt), *Root cause* (with
  file:line references), *Proposed fix*, *Confidence & risks*, *How to verify*.
  If Claude is not confident in a fix, the PR contains analysis only. Log
  excerpts are treated as untrusted data — the prompt forbids following
  instructions embedded in them.

## Costs (per project)

- **Quiet night:** ~1 min of GitHub Actions, zero Claude usage
  (≈30 min/month — free tier is 2,000–3,000 min/month).
- **Error night:** one Claude session, roughly $0.5–$3 per error on API
  billing, or no marginal cost with a subscription token. Hard-capped at 3
  errors/night.

## Development

```bash
pytest scripts/coolify_watchdog/        # parser + fingerprint unit tests
```

`fetch_errors.py` is stdlib-only on purpose — no dependencies — so it runs on
a bare `ubuntu-latest` runner. CI (`.github/workflows/tests.yml`) runs the
test suite on every push. Callers can pin a specific engine version with the
`engine_ref` input (defaults to `main`).
