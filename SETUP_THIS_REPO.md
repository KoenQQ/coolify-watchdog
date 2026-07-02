# One-time: publish this as KoenQQ/coolify-watchdog

Run from inside this directory (needs `gh` logged in, or create the empty repo
in the GitHub UI first and skip the first command):

```bash
gh repo create KoenQQ/coolify-watchdog --public --description "Nightly Coolify error watchdog: log monitoring + Claude-proposed fixes as draft PRs"

git init -b main
git add -A
git commit -m "Coolify error watchdog engine"
git remote add origin https://github.com/KoenQQ/coolify-watchdog.git
git push -u origin main
```

`--public` is the friction-free option (the engine contains no secrets). If
you prefer `--private`, also do the two extra steps in README.md →
"Repo visibility".

Then delete this file. Projects connect per README.md → "Adding the watchdog
to a project" — proflr already has its caller workflow on the
`claude/cuddify-error-log-monitoring-5kubef` branch.
