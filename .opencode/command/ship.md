---
description: Autonomously validate, deploy, verify, then commit and push a homelab module change
agent: build
---

Ship a homelab module change end to end, autonomously, from `$ARGUMENTS`
(`[module] [host]`, both optional). Follow AGENTS.md for the exact
validate/test/deploy/commit/CI commands, the risk tiers, and secret rules — don't
restate them, just run them. Load the `deploy-module` skill for predicate
patterns and stop-reason handling. Do not ask anything mid-run; decide, proceed,
report at the end.

Sequence: infer module (from `git diff` if unnamed) -> state success predicate ->
validate (CI parity) -> dry-run -> capture pre-state -> deploy (canary first) ->
verify -> commit -> push -> confirm CI green -> re-check deploy.

**Preconditions — bail out before doing anything if any fail.** If the working
tree mixes the target module's changes with unrelated dirty files (a different
module, or tooling/docs changes), split the unrelated files into their own
commit first (or leave them uncommitted, untouched) rather than guessing which
files belong to the shipped change — then proceed with the module change alone.
Renames, retirements, and a module's first-ever deploy are fine to ship as long
as the dry-run diff is reviewed (see AGENTS.md Shipping Strategy). The change is
not an in-progress incident response, and the target is not an offsite host
(`cinci`, `cottonwood`). If a precondition still fails after that, stop and say
which one; do not proceed on a guess.

Only these points aren't obvious from AGENTS.md:

- **Success predicate first.** Before deploying, state a specific checkable
  assertion for what the change should make true on the host — a config value
  present, `systemctl show` reflecting a new flag, a timer's next-run matching a
  new schedule. `systemctl is-active` alone is not a predicate; a service runs
  fine on the old config. If no predicate can be stated, STOP before deploying.
- **Capture pre-state.** Before deploy, record the current value of whatever the
  predicate checks, plus enabled/active state of affected units. This is what
  makes verification and recovery meaningful.
- **Blast-radius boundary for auto-fix.** Before deploy (validate, dry-run) —
  auto-fix freely, then re-run from the top of the sequence; cap at 3 attempts,
  then STOP. After deploy touches a host — never auto-fix. Recover and report.
- **Canary before fan-out.** When the target is `all`, deploy to one host,
  verify the predicate there, then proceed to the rest. A canary failure stops
  the run before any other host is touched.
- **Verify failure = diverged host, not just a stop.** If deploy succeeded but
  the predicate failed, the host now differs from both git and its prior state.
  Skip commit+push, and report: what changed on the host, the captured
  pre-state, and whether reverting means re-deploying from HEAD or manual
  intervention. Do not attempt the fix.
- **Risk tier governs the host argument.** Check the module's tier in AGENTS.md
  ("Shipping Strategy") before choosing a target. Tier 3 (control-path:
  `ssh-config`, `keepalived`, `pve-interface-pinning`, `pve-upgrade`, the
  `pve-zfs-*` patches) -> refuse the run and tell the user to deploy manually
  with console access; never ship these, whatever host is named. Tier 2
  (stateful) -> require an explicitly named host; STOP if none was given. Tier 1
  (routine) -> may default to `all`, still canary-first.
- **Transient vs. real failure.** Retry a step at most once, and only for
  recognizably environmental errors (SSH connect timeout/reset, package mirror
  timeout, transient DNS). Never for validation, installer, or config errors.
  Same error on retry means it's real: stop.
- **Config drift is not a failure.** Unrelated diffs in the dry-run (stale
  aliases, other hosts, inventory reconciliation) -> record and continue; the
  intended change still ships.
- **Offsite key.** On cottonwood/cinci, "Private key file is encrypted" means
  tell the user to run `addoffsitekey` — not a code failure.
- **Resumable.** A re-run after an early stop repeats from the top; the sequence
  is idempotent up to the deploy step. Say so in the report.
- **New failure pattern -> propose, don't apply.** Unlisted failure or drift
  pattern -> add a candidate bullet under **Suggested update** in the report for
  the user to merge into AGENTS.md/ship.md. Never edit either file yourself.

End with: what shipped, commit hash, CI status, and the verification result
**quoted as evidence** (the predicate and the actual observed value, not
"verified OK"). Add a **Config drift** note if any was seen, a **Suggested
update** note for new patterns, and if stopped early: which step, why, whether
the failure was real or a transient that exhausted its retry, and whether any
host is left diverged.
