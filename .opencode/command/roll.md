---
description: Pre-flight every PVE node and VM, confirm the whole roll once, then reboot in safe waves
agent: build
---

`./roll plan` -> one confirmation for the whole roll -> reboot wave by wave, `./roll verify` after each -> VMs -> verify

Roll the reboot workflow across [$ARGUMENTS], defaulting to every PVE node plus every
VM that boots its own kernel (`pve-guest` qemu with `metrics-exporters.own_kernel`:
`helm`, `riven`, and any added later). Live migration never clears a VM's pending
kernel, so the node roll alone leaves the VMs behind.

`./roll` is the read-only half of this workflow and the single owner of the survey,
the stop conditions, the wave order and the recovery checks — run it, do not
re-derive what it reports with ad-hoc SSH. `pve-upgrade/README.md` is the runbook:
the why behind the order, the facts it depends on, and the manual fallback if
`./roll` itself is broken. This file owns the authorization gate and the sequencing
of the reboots, which `./roll` never issues.

**Ask once, not per wave.** Survey everything first, present one plan, and — once it
is approved — execute the whole roll without further prompting. Interrupt only when
reality stops matching the approved plan (see step 4). A clean roll should cost the
human exactly one decision.

**Upgrades are already automated; this run installs nothing.** `apt-upgrade`
dist-upgrades every PVE node on Saturday at 05:00–05:15 (and `arc`/`xur` daily at
04:05/04:00), kernel included; Debian security fixes install daily in between. The
trigger for this command is the Saturday 09:00 `RebootRequired` Telegram digest.

1. **Survey, silently.** Run `./roll plan [$ARGUMENTS]` (add `--json` if you want to
   read it structurally). It reads every node even for a subset, because tower's
   placement and HA are cluster facts, and it refuses inside the 02:00 and 08:00
   maintenance windows. Do not ask anything during this phase. Do **not** run
   `./deploy --confirm-upgrade pve-upgrade` during the roll — `apt-upgrade` owns these
   nodes, and an upgrade mid-roll is the unreviewed package change the ordering
   exists to prevent.
2. **Read the verdict.**
   - `nothing-pending` (exit 0): say so and stop — no question, no reboot. That is
     the expected outcome most weeks.
   - `stop` (exit 1): report the stop conditions and stop. Do not ask, do not reboot.
     One stop names its own fix: a **kernel that is available but not installed**.
     Rebooting then would boot the older kernel and owe a second reboot, so the plan
     tells you to install it first (`./deploy --confirm-upgrade pve-upgrade <node>`
     for a node, the host's `homelab-apt-dist-upgrade.service` for a VM). Offer that
     to the human as the next step — it is the one sanctioned upgrade, run *before*
     a roll, never during one — then re-plan.
   - `ready` (exit 0): continue. **Held** VMs (a CI job running, an unhealthy swarm
     peer, unreachable) stay pending and out of this roll; report them, they do not
     block the nodes.
3. **One confirmation authorizes the whole roll.** Use the question tool
   (`AskUserQuestion` under Claude Code) **once**, presenting what the plan printed:
   the waves in order, the guests each node moves or stops, running vs installed
   kernels, every warning, held VMs, and that the node waves end with the node
   running riven (normally `bray`), which live-migrates riven and with it this agent
   session and the shared SSH agent. List the VM reboots after the waves (step 6),
   and say that a riven reboot ends this session. Offer approving the full roll, and
   — when more than one wave or a VM is pending — approving only the earlier waves,
   or only the nodes without the VMs. Declining stops everything. That single
   approval is the authorization for every `systemctl reboot` in the plan; a green
   plan is not.
4. **Execute the approved plan unattended.** For each wave, `ssh <node>
   'systemctl reboot'` for every node in it (wave 1 may pair a standalone node with
   one cluster node; never two cluster nodes), then
   `./roll verify --wait 900 <every node in the wave>` and continue straight into
   the next wave when it exits 0. Report progress as you go, but do not ask again.
   Do **not** enter HA maintenance: `ha: shutdown_policy=migrate` handles HA
   services during a direct reboot, and maintenance would restart every LXC twice
   because LXC cannot live-migrate. A `clovis` reboot live-migrates helm (`vm:112`),
   the monitoring stack, to ace; `./roll verify` asks helm for `up`, so a helm that
   did not come back shows there. If that migration fails, HA restarts helm from its
   replica, and the monitoring blind window until it returns is expected, not an
   incident.

   **Stop and report** — do not silently adapt — when `./roll verify` still fails at
   its deadline. Re-run `./roll plan` before a wave if anything looks different, and
   re-ask only if the roll must deviate from what was approved (a node newly needing
   a reboot, tower having moved such that the wave order is now wrong, or a
   recovered-but-degraded state where continuing is a judgement call). Stopping
   outranks finishing the task.
5. **The controller's node, last.** Its reboot live-migrates riven, and this session
   with it, so issue it only after every earlier wave has verified clean. Then confirm
   `vm:110` is started on ace and verify it like any other wave. If the session does
   not survive, the human starts a new session to run `./roll verify` for it. No extra
   confirmation: step 3 already covered it.
6. **VMs, after every node wave has verified,** in the plan's order (riven last).
   Right before each, re-run `./roll plan <vm>`: its state may have changed during
   the roll, and a VM that is now held is skipped and reported as still pending.
   - **helm**, per the README's `# Rebooting helm`: `ssh helm-root 'systemctl
     reboot'` from inside the guest, then `./roll verify --wait 600 helm`. Expect a
     minute or two with no monitoring.
   - **riven last of all**, per `# Rebooting riven`: a delayed, detached reboot from
     inside the guest. It ends this session. Before issuing it, deliver the final
     report, which tells the human to reconnect, run `addhomelabkeys`, and run
     `./roll verify riven`.
   - Any other own-kernel VM: reboot from inside the guest as root, then
     `./roll verify --wait 600 <vm>`.
   Stop-and-report rules from step 4 apply here too.

Report per node and per VM: whether a reboot was pending, the running vs installed
kernel, and the verification result, plus which waves ran. If nothing was pending, say
so rather than implying work was done — that is the expected outcome most weeks.
