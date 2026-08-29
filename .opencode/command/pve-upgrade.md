---
description: Roll the monthly Proxmox upgrade across the PVE nodes per the runbook
agent: build
---

preflight -> upgrade -> reboot only if required -> verify -> next node

Run the rolling Proxmox upgrade for [$ARGUMENTS], defaulting to every PVE node.

**Read `pve-upgrade/README.md` and follow its per-node procedure exactly.** It is the
canonical runbook and owns the pre-flight commands, the stop conditions, the reasoning
behind the order, and the verification steps. Do not restate or improvise them. This
file only pins what must not be varied.

- **Order:** `osiris`, `bray`, `ace`, `clovis` — one node at a time, never two at once.
  A subset in $ARGUMENTS keeps this relative order.
- **Per node:** the README's pre-flight, then `./deploy --confirm-upgrade pve-upgrade
  <node>`, then the README's verification. The `--confirm-upgrade` flag is required;
  the module refuses without it and is excluded from `./deploy all`.
- **Stop on any failed pre-flight check or failed deploy.** Do not continue to the next
  node. Report which node stopped the run and its observed state. This rule outranks
  finishing the task.
- **Never reboot unattended.** If `homelab_reboot_required` is `1`, stop and report it
  with the node's HA-migration and reboot commands; wait for a human decision. Most
  months no node needs one.
- **Refuse to start** inside the 02:00 or 08:00 maintenance windows — alert suppression
  there would hide problems the upgrade causes.
- `riven` runs on `bray`, so rebooting bray kills this session; say so before that step.
  `clovis` runs the monitoring stack, so a blind window there is expected, not an
  incident.

Report per node: packages upgraded, whether a reboot is pending, and the verification
result. If nothing was pending, say so rather than implying work was done.
