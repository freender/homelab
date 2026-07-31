---
description: Autonomously validate, deploy, verify, then commit and push a homelab module change
agent: build
---

Ship a homelab module change end to end from `$ARGUMENTS` (`[module] [host]`, both
optional). AGENTS.md has the commands, risk tiers, and secret rules — run them, don't
restate them. Load the `deploy-module` skill for predicate patterns. Decide and proceed:
no questions mid-run, report at the end.

**Sequence:** infer module (from `git diff` if unnamed) -> state predicate -> validate ->
dry-run -> capture pre-state -> deploy (canary first) -> verify -> commit -> push ->
confirm CI green -> re-check deploy.

**Stop before starting if** the dirty tree mixes unrelated changes (split them into their
own commit first, then ship the module alone), this is incident response, or the target is
offsite (`cinci`, `cottonwood`). Tier 3 -> refuse, tell the user to deploy manually with
console access. Tier 2 ->
require an explicitly named host. Name the failed precondition; never proceed on a guess.

Beyond AGENTS.md:

- **Predicate first.** Before deploying, state a checkable assertion the change should
  make true on the host: a config value present, `systemctl show` reflecting a new flag, a
  timer's next-run matching a new schedule. `is-active` is not a predicate — a service
  runs fine on the old config. If none can be stated, STOP before deploying.
- **Capture pre-state** of whatever the predicate checks, plus affected units'
  enabled/active state. This is what makes verification and recovery meaningful.
- **Auto-fix boundary.** Before deploy: fix freely and re-run from the top, cap 3
  attempts then STOP. Once deploy has touched a host: never. Recover and report.
- **Canary.** Target `all` -> one host, verify the predicate there, then the rest. A
  canary failure stops the run before any other host is touched.
- **Deploy OK + predicate failed = diverged host.** Skip commit and push. Report what
  changed on the host, the captured pre-state, and whether recovery is a re-deploy from
  HEAD or manual intervention. Do not attempt the fix.
- **Retry once, only for environmental errors** (SSH connect timeout/reset, package
  mirror timeout, transient DNS) — never validation, installer, or config errors. The
  same error twice is real: stop.
- **Config drift is not failure.** Unrelated dry-run diffs (stale aliases, other hosts,
  inventory reconciliation) -> record and continue; the intended change still ships.
- **Offsite key.** "Private key file is encrypted" -> tell the user to run
  `addoffsitekey`; it is not a code failure.
- **Resumable.** A re-run after an early stop repeats from the top; the sequence is
  idempotent up to the deploy step. Say so in the report.
- **New failure pattern -> propose, don't apply.** Add a candidate bullet under
  **Suggested update**; never edit AGENTS.md or ship.md yourself.

Report: what shipped, commit hash, CI status, and the predicate **quoted with its observed
value** (not "verified OK"). Add **Config drift** and **Suggested update** notes if either
applies. If stopped early: which step, whether the failure was real or a transient that
exhausted its retry, and whether any host is left diverged.
