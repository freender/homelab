---
description: Validate, deploy, verify, commit, and push a homelab module change
agent: build
---

validate -> dry-run -> deploy/canary -> verify -> commit -> push -> CI

Ship scope [$ARGUMENTS] end to end, inferring omitted `[module] [host]` values. Load
`deploy-module` for CLI mechanics, decide routine details without questions, and report at
the end.

Invocation is the human decision to run a live deploy, so there are no separate module
risk tiers — but do not skip a step to get there faster.

1. **Validate.** Infer the module from the arguments or related working-tree changes and
   run `./validate`. Fix a direct in-scope failure and rerun; otherwise stop. Unrelated
   dirty files do not block shipping and must not be staged.
2. **Dry-run.** Run `./deploy --dry-run <module> <host>` and read the diff. Stop on an
   unresolved failure. Record unrelated config drift and continue.
3. **Deploy/canary.** Deploy a named host directly. For `all`, deploy and verify one
   suitable host before the rest. Offsite targets are allowed when their key is loaded;
   skip and report them when the key is unavailable or encrypted.
4. **Verify.** Check the specific value or behavior changed, not only service activity.
   Capture pre-state when useful, but it is not mandatory. For renames or retirements,
   also verify that the old object is gone. Deploy success plus verification failure means
   the host is **diverged**: stop before commit and push and report the observed state.
5. **Commit.** Stage only files belonging to the requested change and create a concise
   commit.
6. **Push.** Push the commit; stop and report if the push fails.
7. **CI.** Watch the matching Actions run through completion and investigate failures.

Report what shipped or was skipped, the verification and observed value, commit hash, and
CI status. If stopped, name the failed step and whether a host was left diverged.
