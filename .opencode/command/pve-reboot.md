---
description: Pre-flight every PVE node, confirm the whole roll once, then reboot in safe waves
agent: build
---

survey all nodes -> one confirmation for the whole roll -> reboot wave by wave -> verify

Roll the reboot workflow across [$ARGUMENTS], defaulting to every PVE node.
`pve-upgrade/README.md` is the canonical runbook and owns the pre-flight commands,
the stop conditions, and the verification steps — follow it rather than restating or
improvising it. This file owns the wave policy and the authorization gate.

**Ask once, not per wave.** Survey everything first, present one plan, and — once it
is approved — execute the whole roll without further prompting. Interrupt only when
reality stops matching the approved plan (see step 4). A clean roll should cost the
human exactly one decision.

**Upgrades are already automated; this run installs nothing.** `apt-upgrade`
dist-upgrades every PVE node daily at 05:00–05:15 (and `arc`/`xur` at 04:05/04:00),
kernel included. The trigger for this command is the Saturday 09:00 `RebootRequired`
Telegram digest.

**Refuse to start** inside the 02:00 or 08:00 maintenance windows — alert suppression
there would hide problems a reboot causes.

1. **Survey every node up front, silently.** For each in-scope node run the README's
   pre-flight, the step-2 automation-state read, and the `homelab_reboot_required`
   check. Do not ask anything during this phase and do not report progress node by
   node; it is read-only and its only product is the plan in step 3. Do **not** run
   `./deploy --confirm-upgrade pve-upgrade` — `apt-upgrade` already owns these nodes
   and that module is now only an on-demand escape hatch (chiefly for `arc`/`xur`).
   Running it here would dist-upgrade a node mid-runbook, which is exactly the
   unreviewed package change the ordering exists to prevent.
2. **Order.** Choose the three waves from tower's **current** HA placement:
   `ssh ace 'ha-manager status'` reports `ct:101` (tower)'s node. If tower is on ace:
   **`osiris` + `clovis`**, then **`ace`**, then **`bray`**. Otherwise: **`osiris` +
   `ace`**, then **`clovis`**, then **`bray`**. A requested subset preserves that
   selected order. osiris is standalone and holds no corosync vote, so pairing it with
   either ace or clovis is one cluster node down, not two — never take two *cluster*
   nodes (`ace`/`bray`/`clovis`) at once. This leaves the node currently carrying tower
   alone. **`bray` is last because `riven` lives there:** rebooting it kills the agent
   session and empties the shared SSH agent, so it must happen when nothing is left to
   orchestrate. Drop any node whose `homelab_reboot_required` is `0`; an emptied wave
   disappears rather than being "skipped" later.
3. **One confirmation authorizes the whole roll.** If nothing is pending, say so and
   stop — no question, no reboot. Otherwise use the question tool (`AskUserQuestion`
   under Claude Code) **once**, presenting: the nodes that need a reboot in wave order,
   the guests each one moves or stops, running vs installed kernels, any pre-flight
   warning that did not rise to a stop condition, and that the run ends with `bray`,
   terminating this agent session and emptying the shared SSH agent. Offer approving
   the full roll, and — when more than one wave is pending — approving only the earlier
   waves. Declining stops everything. That single approval is the authorization for
   every `systemctl reboot` in the plan; a green pre-flight is not. If the survey found
   a stop condition, do not ask at all — report it and stop.
4. **Execute the approved plan unattended.** Reboot wave by wave; after each, wait for
   every node to return, verify the README's recovery conditions, wait for HA to settle,
   and continue straight into the next wave. Report progress as you go, but do not ask
   again. Do **not** enter HA maintenance: `ha: shutdown_policy=migrate` handles HA
   services during a direct reboot, and maintenance would restart every LXC twice
   because none can live-migrate. `clovis` runs the monitoring stack, so the blind
   window during its reboot is expected, not an incident.

   **Stop and report** — do not silently adapt — on any failed recovery check, a node
   that does not return, a pool that comes back degraded, quorum not restored, or a
   `homelab_reboot_required` still `1` after its reboot. Re-ask only if the roll must
   deviate from what was approved (a node newly needing a reboot, tower having moved
   such that the wave order is now wrong, or a recovered-but-degraded state where
   continuing is a judgement call). Stopping outranks finishing the task.
5. **bray, last.** Its reboot terminates this session, so issue it only after every
   earlier wave has verified clean, then stop — the human starts a new session to
   verify bray. No extra confirmation: step 3 already covered it.

Report per node: whether a reboot was pending, the running vs installed kernel, and the
verification result, plus which waves ran. If nothing was pending, say so rather than
implying work was done — that is the expected outcome most weeks.
