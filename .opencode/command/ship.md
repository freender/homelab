---
description: Dry-run, deploy, test, then commit and push a homelab module change if all checks pass
agent: build
---

You are shipping a homelab module change end to end. The user invoked `/ship`
with arguments: `$ARGUMENTS`

Interpret `$ARGUMENTS` as `[module] [host]` (either may be omitted):
- If a module is named, target that module. If not, infer the module(s) from
  the current `git status`/`git diff` (which modules have uncommitted changes)
  and confirm with the user before proceeding.
- If a host is named, target that host. If not, default to a safe single host
  that actually runs the module (check `hosts.conf` and
  `./deploy --dry-run <module> all`), and state which host you picked and why.
  Never silently deploy to `all` hosts — if the user wants `all`, they must say so.

Do NOT fix bugs automatically. If any step fails, STOP, report the failure with
the relevant output, and ask the user how to proceed. Never push a fix you were
not explicitly asked to make.

Work through these steps in order, using a todo list to track them:

1. **Show scope.** Run `git status --short` and `git diff --stat`. Summarize what
   changed and the module/host you will target. If anything is ambiguous, ask.

2. **Local validation (CI parity).** Run:
   - `.venv/bin/python -m ruff check <changed python files>` (or the whole
     `src/` if broad)
   - `shellcheck -S warning <changed shell scripts>`
   - `.venv/bin/python -m pytest tests/ -q`
   - `./validate`
   If any fail, STOP and report.

3. **Dry-run deploy.** Run `./deploy --dry-run <module> <host>` and show the
   output. Confirm it does what the change intends (e.g. the right pause/enable
   behavior, no unexpected diffs). If the dry-run reveals a problem, STOP.

4. **Real deploy.** Run `./deploy <module> <host>`. Show the output. If it fails
   (SSH, installer error, etc.), STOP and report. Note: offsite hosts
   (cottonwood, cinci) may need the offsite SSH key — if you hit
   "Private key file is encrypted", tell the user to run `addoffsitekey` rather
   than treating it as a code failure.

5. **Post-deploy verification.** Verify the change actually works on the host,
   appropriate to the module. For service/timer modules, SSH in and check
   `systemctl is-enabled`/`is-active` for the managed units and
   `systemctl --failed`. For zfs-automation specifically, also confirm the
   relevant timers and, when safe, run a oneshot service (e.g.
   `systemctl start homelab-zfs-snapshots.service`) and check its `Result`.
   Report exactly what you checked and the outcome. If verification fails, STOP.

6. **Commit.** Only if steps 1–5 all passed. Inspect `git status`, `git diff`,
   and `git log --oneline -6` for style. Stage the intended files (never
   secrets, `.env`, or `build/` artifacts). Write a concise commit message that
   matches the repo's style (lowercase `module: summary` subject line). Do not
   commit unrelated changes. If the working tree contains inventory test edits
   (e.g. a temporary `paused: true` you added to `hosts.conf` for testing),
   make sure they are reverted before committing.

7. **Push.** `git push`. Then verify CI: wait ~20s and run
   `gh run list --branch main --limit 3`. Confirm the `Validate` run for your
   commit is `success`. If it is red, run `gh run view --log-failed` on that run
   and report the failure — do not attempt a fix unless the user asks.

8. **Final re-check.** After push, run `./deploy <module> <host>` once more to
   confirm the pushed state still deploys cleanly, and report the final unit
   state.

End with a short summary: what shipped, the commit hash, CI status, and the
verified on-host result. If you stopped early, clearly state at which step and
why, and what you need from the user.
