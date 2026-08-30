---
description: Roll the monthly Proxmox upgrade across the PVE nodes per the runbook
agent: build
---

preflight -> upgrade -> reboot check (hand off if required) -> verify -> next node

Run the rolling Proxmox upgrade for [$ARGUMENTS], defaulting to every PVE node.
Follow AGENTS.md's **PVE Upgrade (`/pve-upgrade`)** section for every step and
`pve-upgrade/README.md` for the runbook itself. Never reboot and never migrate a node's
guests — hand off to the human instead. Report per node at the end.
