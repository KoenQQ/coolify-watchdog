---
name: setup-coolify-watchdog
description: Install the nightly Coolify error watchdog into the current repository. Use when the user wants automatic nightly log monitoring with Claude-proposed fixes as draft PRs. Copies the caller workflow, collects the Coolify app UUID(s), and prints the secrets/variables checklist.
---

# Set up the Coolify error watchdog

This skill installs the nightly error watchdog into the current repository.
The engine (log parsing, fingerprinting, Claude analysis) lives centrally in
`KoenQQ/coolify-watchdog`; this repo only gets a thin caller workflow.

## Steps

1. **Confirm context.** Check the repo is on GitHub and its app runs on the
   Coolify instance. Ask the user for:
   - The Coolify app UUID(s) to watch (visible in the Coolify UI URL when the
     application is open, or via `GET /api/v1/applications`). Ask for a short
     name per app, e.g. `backend`.
   - The Coolify base URL (default: `https://apps.pompadour.ventures`).

2. **Install the caller workflow.** Write `.github/workflows/coolify-watchdog.yml`
   with the exact content of the template below, no modifications needed —
   app config lives in repo variables, not in the file.

3. **Optionally add a local ignore list.** Create `scripts/coolify_watchdog/ignore.txt`
   (comment header only) so noisy known errors can be silenced later by adding
   their 10-char fingerprint on a line.

4. **Print the configuration checklist** for the user to complete in
   GitHub → repo → Settings → Secrets and variables → Actions:
   - Secret `COOLIFY_API_TOKEN` — Coolify UI → Keys & Tokens → API tokens.
   - Secret `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` (from
     `claude setup-token`, uses the Claude Pro/Max subscription).
   - Secret `ENGINE_REPO_TOKEN` — fine-grained PAT with read (Contents) access
     to `KoenQQ/coolify-watchdog`; only needed while that repo is private.
   - Variable `COOLIFY_API_URL` — the Coolify base URL.
   - Variable `COOLIFY_APPS` — JSON, e.g.
     `[{"name":"backend","uuid":"<uuid-from-step-1>"}]`.

5. **Verify.** Once secrets are set, run the workflow manually with
   `dry_run: true` (Actions tab → Coolify Error Watchdog → Run workflow) and
   check the job summary lists the errors found (or none) without opening PRs.

## Caller workflow template

```yaml
name: Coolify Error Watchdog

on:
  schedule:
    - cron: "30 2 * * *"
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Only report errors — do not run Claude or open PRs"
        type: boolean
        default: false

permissions:
  contents: write
  pull-requests: write

jobs:
  watchdog:
    uses: KoenQQ/coolify-watchdog/.github/workflows/coolify-watchdog.yml@main
    with:
      dry_run: ${{ inputs.dry_run == true }}
      coolify_api_url: ${{ vars.COOLIFY_API_URL }}
      coolify_apps: ${{ vars.COOLIFY_APPS }}
    secrets:
      COOLIFY_API_TOKEN: ${{ secrets.COOLIFY_API_TOKEN }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      ENGINE_REPO_TOKEN: ${{ secrets.ENGINE_REPO_TOKEN }}
```

## Notes

- If `KoenQQ/coolify-watchdog` is private, it must have: Settings → Actions →
  General → Access → "Accessible from repositories owned by the user".
  If it is public, no extra access setup is needed and `ENGINE_REPO_TOKEN`
  can be omitted.
- Each new error becomes ONE draft PR on a `watchdog/<date>-<fingerprint>`
  branch, labeled `log-watchdog`, assigned to the repo owner. Closing the PR
  without deleting the branch keeps the error muted; deleting branch + PR makes
  the watchdog retry it.
