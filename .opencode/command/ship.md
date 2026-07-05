---
description: Autonomously validate, deploy, verify, then commit and push a homelab module change
agent: build
---

Ship a homelab module change end to end, autonomously, from `$ARGUMENTS`
(`[module] [host]`, both optional). Follow AGENTS.md for the exact
validate/test/deploy/commit/CI commands and secret rules — don't restate them,
just run them. Do not ask anything mid-run; decide, proceed, report at the end.

Sequence: infer module (from `git diff` if unnamed) -> validate (CI parity) ->
dry-run -> deploy -> verify on host -> commit -> push -> confirm CI green ->
re-check deploy.

Only these points aren't obvious from AGENTS.md:

- **Host default.** If no host is named: file-reconciliation modules (e.g.
  `ssh-config`) -> `all` (deploying everywhere is how drift gets cleared).
  Service/stateful modules (docker, zfs-automation, pbs-client-backup, etc.) ->
  one safe host that runs the module; never fan out to `all` unless named.
- **Stop only on real failure.** Validation error, deploy/SSH/installer error, or
  failed on-host verification -> STOP, skip commit+push, report. Do not auto-fix.
- **Config drift is not a failure.** Unrelated diffs in the dry-run (stale
  aliases, other hosts, inventory reconciliation) -> record and continue; the
  intended change still ships.
- **Offsite key.** On cottonwood/cinci, "Private key file is encrypted" means
  tell the user to run `addoffsitekey` — not a code failure.

End with: what shipped, commit hash, CI status, on-host verification result, and a
**Config drift** note if any was seen (reconciled by this deploy, or left for
follow-up). If stopped early: which step, why, what's needed.
