---
description: Autonomously dry-run, deploy, verify, then commit and push a homelab module change
agent: build
---

Ship a homelab module change end to end, autonomously. Args (`$ARGUMENTS`) are
`[module] [host]`, either optional. Do not ask the user anything mid-run; decide
and proceed, then report at the end.

Resolve targets:
- Module: use the named one, else infer from `git status`/`git diff` (changed
  module(s)).
- Host: use the named one, else pick a default that fits the module:
  - Pure file-reconciliation modules (e.g. `ssh-config`) -> default to `all`,
    since deploying everywhere is the correct way to detect and clear config
    drift across every consumer host.
  - Service/timer/stateful modules (docker, zfs-automation, pbs-client-backup,
    apt-upgrade, etc.) -> default to one safe host that runs the module (from
    `hosts.conf` / `./deploy --dry-run <module> all`); do not fan out to `all`
    unless the user named `all`.

Do NOT auto-fix bugs. Only STOP (skip commit+push) on a real failure: validation
error, deploy/SSH/installer error, or failed post-deploy verification. Config
drift is NOT a failure — keep going and report it at the end.

Steps (track with a todo list):

1. **Scope.** `git status --short`, `git diff --stat`. Note module + chosen host.
2. **Validate (CI parity).** `ruff check` (changed py, or `src/`), `shellcheck -S
   warning` (changed sh), `pytest tests/ -q`, `./validate`. Any fail -> STOP.
3. **Dry-run.** `./deploy --dry-run <module> <host>`. Confirm the intended change
   is present. If the target diff also contains unrelated changes (stale aliases,
   other hosts, inventory reconciliation), that is **drift**: record it for the
   final report and continue. For file-reconciliation modules deployed to `all`,
   this drift is expected and gets reconciled by the deploy — that is the point.
4. **Deploy.** `./deploy <module> <host>`. Failure -> STOP. Offsite hosts
   (cottonwood, cinci): "Private key file is encrypted" means tell the user to run
   `addoffsitekey`, not a code failure.
5. **Verify on host.** Check what the module delivers: service/timer modules ->
   `systemctl is-enabled`/`is-active` + `systemctl --failed`; zfs-automation ->
   timers and a safe oneshot (`systemctl start homelab-zfs-snapshots.service`,
   check `Result`); file-only modules (e.g. ssh-config) -> confirm the deployed
   file/state is live and correct. Fail -> STOP.
6. **Commit.** Only if 1–5 passed. Check `git diff` + `git log --oneline -6` for
   style. Stage only intended files (never secrets, `.env`, `build/`). Revert any
   temporary test edits (e.g. a test-only `paused: true`). Lowercase
   `module: summary` subject.
7. **Push + CI.** `git push`; wait ~20s; `gh run list --branch main --limit 3`.
   If the `Validate` run is red, `gh run view --log-failed` and report — do not fix
   unless asked.
8. **Re-check.** `./deploy <module> <host>` once more; confirm clean.

Final summary: what shipped, commit hash, CI status, on-host verification result.
Add a **Config drift** section if any was detected (host(s), what else changed,
and whether it was reconciled by this deploy or left for follow-up). If stopped
early, state the step, why, and what's needed.
