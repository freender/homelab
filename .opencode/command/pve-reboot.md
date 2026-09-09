---
description: Confirm and roll a reboot across the PVE nodes in safe waves
agent: build
---

preflight -> reboot check -> confirm each wave -> reboot -> recover -> next wave

Roll the reboot workflow across [$ARGUMENTS], defaulting to every PVE node.
`pve-upgrade/README.md` is the canonical runbook and owns the pre-flight commands,
the stop conditions, and the verification steps — follow it rather than restating or
improvising it. This file owns the wave policy and the authorization gate.

**Upgrades are already automated; this run installs nothing.** `apt-upgrade`
dist-upgrades every PVE node daily at 05:00–05:15 (and `arc`/`xur` at 04:05/04:00),
kernel included. The trigger for this command is the Saturday 09:00 `RebootRequired`
Telegram digest.

**Refuse to start** inside the 02:00 or 08:00 maintenance windows — alert suppression
there would hide problems a reboot causes.

1. **Order.** Choose the three waves from tower's **current** HA placement before any
   pre-flight: `ssh ace 'ha-manager status'` reports `ct:101` (tower)'s node. If tower
   is on ace: **`osiris` + `clovis`**, then **`ace`**, then **`bray`**. Otherwise:
   **`osiris` + `ace`**, then **`clovis`**, then **`bray`**. A requested subset
   preserves that selected order. osiris is standalone and holds no corosync vote, so
   pairing it with either ace or clovis is one cluster node down, not two — never take
   two *cluster* nodes (`ace`/`bray`/`clovis`) at once. This leaves the node currently
   carrying tower alone. **`bray` is last because `riven` lives there:** rebooting it
   kills the agent session and empties the shared SSH agent, so it must happen when
   nothing is left to orchestrate.
2. **Per-node scope is exactly three things:** the README's pre-flight, the
   `homelab_reboot_required` check, and the README's verification. Do **not** run
   `./deploy --confirm-upgrade pve-upgrade` as part of this — `apt-upgrade` already owns
   these nodes, and that module is now only an on-demand escape hatch (chiefly for
   `arc`/`xur`). Running it here would dist-upgrade a node mid-runbook, which is exactly
   the unreviewed package change the ordering exists to prevent. If any check fails,
   stop. If no node in a wave needs a reboot, skip that wave.
3. **Confirmation is the authorization.** After a clean pre-flight and a positive
   `homelab_reboot_required` check for one or more nodes in a wave, use the question
   tool (`AskUserQuestion` under Claude Code) to present the exact nodes needing a
   reboot, the guests affected, running vs installed kernels, and expected impact. Only
   an explicit confirmation authorizes those nodes' `systemctl reboot`. A green
   pre-flight does not. A declined wave stops the whole run; do not proceed to a later
   wave with an earlier one deliberately left pending. Do **not** enter HA maintenance:
   `ha: shutdown_policy=migrate` handles HA services during a direct reboot, and
   maintenance would restart every LXC twice because none can live-migrate.
4. **Recover before continuing.** After a confirmed reboot, wait for every node in the
   wave to return, verify the README's recovery conditions, and wait for HA to settle
   before asking about the next wave. Do not begin a later wave on timeout or partial
   recovery. Stop on any failed pre-flight or recovery check; this outranks finishing
   the task. `clovis` runs the monitoring stack, so the blind window during its reboot
   is expected, not an incident.
5. **bray, last.** Before asking for bray's confirmation, state that rebooting it
   terminates this agent session and empties the shared SSH agent. Execute the reboot
   only after confirmation, then stop — the human starts a new session to verify bray.

Report per node: whether a reboot was pending, the running vs installed kernel, and the
verification result, plus every confirmed or skipped wave. If nothing was pending, say
so rather than implying work was done — that is the expected outcome most weeks.
