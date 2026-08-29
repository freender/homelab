---
description: Roll the monthly Proxmox upgrade across the PVE nodes per the runbook
agent: build
---

preflight -> upgrade -> reboot check (hand off if required) -> verify -> next node

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
- **Never reboot. The reboot is always a manual human step.** If
  `homelab_reboot_required` is `1`, stop at that node, report it with the README's
  HA-migration and reboot commands, and leave the node up and unmigrated. Do not
  reboot it yourself under any circumstances, and do not treat a clean pre-flight as
  authorization. Most months no node needs one.
- **Do not touch a node's guests at all.** HA maintenance mode belongs to the manual
  reboot step (README 3a), not to this run — entering it shuts down and restarts every
  LXC on the node, twice, since none can live-migrate. Your scope per node is exactly:
  pre-flight, one `./deploy --confirm-upgrade`, the reboot check, and verification.
- **Refuse to start** inside the 02:00 or 08:00 maintenance windows — alert suppression
  there would hide problems the upgrade causes.
- `riven` runs on `bray` and hosts both this session and the shared SSH agent. You will
  not reboot or migrate bray, so a normal run never disturbs it — but if bray's reboot
  check comes back `1`, say plainly in the handoff that the human's reboot will drop
  this session and empty the SSH agent. `clovis` runs the monitoring stack, so the blind
  window during its manual reboot is expected, not an incident.

Report per node: packages upgraded, whether a reboot is pending, and the verification
result. If nothing was pending, say so rather than implying work was done.
