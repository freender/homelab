---
description: Confirm and roll a reboot across the PVE nodes in safe waves
agent: build
---

preflight -> reboot check -> confirm each wave -> reboot -> recover -> next wave

Roll the reboot workflow across [$ARGUMENTS], defaulting to every PVE node.
Follow AGENTS.md's **PVE Reboot (`/pve-reboot`)** section for every step and
`pve-upgrade/README.md` for the runbook itself. Upgrades are already automated by
`apt-upgrade`, so this run installs nothing.

Choose the three waves from tower's **current** HA placement before doing any
pre-flight: `ssh ace 'ha-manager status'` reports `ct:101` (tower)'s node.
If tower is on ace, use `osiris` + `clovis`, then `ace`, then `bray`. Otherwise
use `osiris` + `ace`, then `clovis`, then `bray`; bray is always last. This
leaves the node carrying tower by itself when it is ace or clovis, while osiris
(which holds no corosync vote) pairs safely with the other node. A requested
subset preserves the selected order. Never take two cluster nodes (`ace`,
`bray`, `clovis`) down at once.

For each wave, complete the README's pre-flight and reboot checks for every node
in it. If any check fails, stop. If no node needs a reboot, skip that wave. If
the human declines a wave, stop the whole run rather than proceeding to a later
wave with an earlier one intentionally left pending.
Before any reboot, use the `question` tool to show the exact nodes, guests affected,
current kernel state and expected impact, and ask the human to confirm that one wave.
Only an explicit confirmation authorizes `systemctl reboot`; a green pre-flight does
not. Do not use HA maintenance mode: `ha: shutdown_policy=migrate` handles HA services
when a cluster node reboots.

After a confirmed wave, wait for every node to return, verify the README's recovery
conditions and wait for HA to settle before asking about the next wave. Do not begin a
later wave on timeout or partial recovery. Before the final bray confirmation, state
that rebooting bray kills this OpenCode session and shared SSH agent; execute the reboot
only after confirmation, then stop. The human starts a new session to verify bray.
Report observed state and every confirmed or skipped wave.
