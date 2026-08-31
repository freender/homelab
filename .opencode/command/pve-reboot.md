---
description: Roll a reboot across the PVE nodes in runbook order, one at a time
agent: build
---

preflight -> reboot check -> hand off if required -> verify -> next node

Roll the reboot check across [$ARGUMENTS], defaulting to every PVE node.
Follow AGENTS.md's **PVE Reboot (`/pve-reboot`)** section for every step and
`pve-upgrade/README.md` for the runbook itself. Upgrades are already automated by
`apt-upgrade`, so this run installs nothing — it decides, per node, whether a reboot is
owed and hands that reboot to the human. Never reboot and never migrate a node's guests.
Report per node at the end.
