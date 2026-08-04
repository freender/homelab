---
description: Validate, deploy, verify, commit, and push a homelab module change
agent: build
---

validate -> dry-run -> deploy/canary -> verify -> commit -> push -> CI

Ship scope [$ARGUMENTS] end to end, inferring omitted `[module] [host]` values.
Follow AGENTS.md's **Shipping (`/ship`)** section for every step. Load `deploy-module` for
CLI mechanics, decide routine details without questions, and report at the end.
