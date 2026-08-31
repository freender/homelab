# Homelab Agent Guide (AGENTS.md)

Python-orchestrated automation for a Proxmox homelab. Python + Bash + YAML; inventory in
`hosts.conf`; local orchestration in `src/homelab/`; shared remote libs in `lib/utils.sh`
and `lib/print.sh`. Pattern: a Python module builds and stages files, then runs a remote
`scripts/install.sh`.

- **Repo work** (edit `src/homelab/`, add/modify a module, `./validate`, or `./deploy`
  dry-run or live) -> the commands below plus the `deploy-module` skill. Topology isn't
  needed; `./deploy` reads `hosts.conf` itself.
- **Live operation** (SSH to inspect/debug a host, cross-host investigation not covered by
  `./deploy`) -> the `homelab-infra` skill for topology, then the relevant topic skill.

## Public Repo Boundary

**`freender/homelab` is a public GitHub repo.** Everything committed is world-readable and
permanent, including history. Treat every edit as publication.

**Never commit:**
- The real public domain used for homelab service routes, or any externally reachable
  route host/URL — not in docs, comments, tests, or CI config. It appears zero times
  today; keep it that way. Use `example.net` placeholders, as templates already do
  (`traefik-tower.example.net`).
- Secrets in any form: `.env`, rendered secret files, tokens, API keys, passwords, SSH
  private keys, PBS encryption keys **or their escrow locations**.
- Third-party detail for `cinci`/`cottonwood` beyond what inventory needs — physical
  location, LAN topology, the owners' personal data. These are other people's networks.
- Live security posture: WireGuard/Pi-KVM route topology, Crowdsec/middleware allowlists,
  per-host SSH auth matrices, agent socket paths, credential file locations.

**Intentionally public — do NOT "sanitize" these:** `*.freender.internal` hostnames,
RFC1918 IPs, usernames, NIC MACs, SSH *public* keys, PBS account/token *names*, and
schedules. `hosts.conf` depends on them; scrubbing them breaks deploys. `.internal` is
split-horizon DNS resolvable only on the LAN, and RFC1918 is unroutable — both are deploy
metadata, not attack surface. Do not confuse `.internal` with the public route domain
above, which is the one that must never appear here. The leak check ignores `.internal`,
`.local`, `.lan`, `.invalid`, and `.test` by design, and allows vendor URLs
(`github.com`, `download.proxmox.com`, `get.docker.com`, `api.telegram.org`).

**Skills:** only repo-scoped tooling docs belong here (`.opencode/skill/deploy-module/`).
Topology, storage, backup, SSH, offsite, monitoring, and secret-handling skills stay
host-local in `~/.config/opencode/skills/` — they are credential and recon maps, not repo
documentation.

`./validate` enforces the domain/secret half of this mechanically (`leak check`); the
judgment calls above are still yours.

## Finding Things — And Not Crawling For Them

- Host facts, SSH metadata, deploy targets, feature config -> `hosts.conf` first.
- Topology, VLANs, storage layout, heavy-path warnings, cross-host context -> the
  `homelab-infra` skill (self-contained topology map).
- Module changes -> read the orchestrator in `src/homelab/modules/` and the matching
  `<module>/scripts/install.sh` before editing.
- Docker app placement and compose definitions -> the repo copy under `docker/`; don't
  inspect live `/mnt/cache/appdata` unless a task requires a known app path.
- **Never** run broad recursive scans on a homelab host under `/`, `/mnt/*`,
  `/mnt/cache`, `/mnt/tank`, `/vm-flash`, `/backup`, or `/srv/timemachine`. `find .` is
  repo-root only — never adapt it to remote storage, media, backup, or appdata paths.
- Unknown path -> ask, or check the Homelab Obsidian docs. Don't discover by crawling.

## Build, Lint, and Test

```bash
./validate                                            # everything CI gates on — best pre-PR check
./deploy --dry-run apcupsd ace                        # single module dry-run (primary "single test")
./deploy --dry-run all all                            # full dry-run
.venv/bin/python -m pytest tests/                     # unit tests
.venv/bin/python -m ruff check src/homelab/cli.py     # targeted lint
shellcheck -S warning pve-postinstall/scripts/install.sh
find . -name '*.sh' -not -path './.bin/*' -exec shellcheck -S warning {} +   # repo root only
yq eval '.' hosts.conf >/dev/null
```

`./validate` runs Python compile, Ruff, Pytest, `hosts.conf` parse validation, the
inventory/module cross-check, the leak check, ShellCheck, and module dry-runs — the same
set CI runs on push/PR to `main` (`.github/workflows/validate.yml`). Ruff and Pytest are
skipped with a warning when missing,
so run it from the repo `.venv` (or `uv run`) for true CI parity. After any push, check
that push's Actions run and inspect failures immediately if any job is red.

The `deploy-module` skill carries the test coverage map (golden renders, pause semantics,
network-critical modules) and which test owns which area; update tests when touching them.

## Layout

- `src/homelab/modules/*.py` — local orchestrator for one module.
- `*/scripts/install.sh` — remote installer for the staged bundle.
- `*/templates` — files rendered from Jinja `{{ VAR }}`; `*/configs` — static copies.
- `*/build` — generated output (gitignored).
- `secrets/` — 1Password-backed deploy-time catalog/templates only; no plaintext `.env`.

## Deploy, Disable, and Pause

Three distinct "off/freeze" switches in `hosts.conf` — do not conflate them:

- **`deploy: false`** (host-level feature gate; formerly `enabled: false`) — removes the
  host from the module's deploy targets: module skipped, running service **never
  touched**. `deploy` wins if both are present; legacy `enabled: false` warns.
- **`<feature>.paused: true`** (module-wide) — stays deployed, but its managed systemd
  units are **stopped and disabled**; reversible. Supported by `disk-spindown`,
  `apt-upgrade`, `pbs-client-backup`, `zfs-automation`.
- **Per-job `paused: true`** (fine-grained) — pauses one unit while others run (e.g.
  `zfs-automation.replication_jobs.<job>.paused`). Distinct from that job's
  `enabled: false`, which retires it entirely (unit files removed).

Implementation how-to (Python flag read, the `homelab_apply_pause` bash helper, unit-file
semantics): `deploy-module` skill.

## Shipping (`/ship`)

`.opencode/command/ship.md` runs validate -> dry-run -> deploy/canary -> verify -> commit ->
push -> CI. It is always invoked explicitly, so invocation is the human decision to run a
live deploy; it does not impose separate module risk tiers.

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

## PVE Reboot (`/pve-reboot`)

**Upgrades are automated; reboots are human-authorized per wave.** `apt-upgrade`
dist-upgrades every PVE node daily at 05:00–05:15 (and `arc`/`xur` at 04:05/04:00),
kernel included. This command installs nothing. It performs the reboot only after a
human explicitly confirms the current wave in the `question` tool; its trigger is the
Saturday 09:00 `RebootRequired` Telegram digest.

`.opencode/command/pve-reboot.md` rolls that across the nodes. `pve-upgrade/README.md`
is the canonical runbook and owns the pre-flight commands, the stop conditions, the
ordering rationale, and the verification steps — follow it rather than restating or
improvising it. What must not be varied:

1. **Order.** Choose the three waves from tower's current HA placement at the start of
   the run (`ha-manager status`, `ct:101`). If tower is on ace: **`osiris` +
   `clovis`**, then **`ace`**, then **`bray`**. Otherwise: **`osiris` + `ace`**, then
   **`clovis`**, then **`bray`**. A requested subset preserves that selected order.
   osiris is standalone and holds no corosync vote, so pairing it with either ace or
   clovis is one cluster node down, not two — never take two *cluster* nodes
   (ace/bray/clovis) at once. This leaves the node currently carrying tower alone.
   Do not start a wave until the previous one is fully back and HA has finished
   rebalancing. **`bray` is last because `riven` lives there:** rebooting it kills the
   agent session and empties the shared SSH agent, so it must happen when nothing is
   left to orchestrate.
2. **Per-node scope is exactly three things:** the README's pre-flight, the reboot check,
   and the README's verification. Do **not** run `./deploy --confirm-upgrade pve-upgrade`
   as part of this — `apt-upgrade` already owns these nodes, and that module is now only
   an on-demand escape hatch (chiefly for `arc`/`xur`). Running it here would dist-upgrade
   a node mid-runbook, which is exactly the unreviewed package change the ordering exists
   to prevent.
3. **Confirmation is the authorization.** After a clean pre-flight and a positive
   `homelab_reboot_required` check for one or more nodes in a wave, present the exact
   nodes needing a reboot, affected guests, running vs installed kernels and expected
   impact through the `question` tool. Only an explicit confirmation authorizes those
   nodes' reboot. A green pre-flight does not. A declined wave stops the run; do not
   proceed to a later wave with an earlier one deliberately left pending. Do **not**
   enter HA maintenance: `ha: shutdown_policy=migrate` handles HA services during a
   direct reboot, and maintenance would restart every LXC twice because none can
   live-migrate.
4. **Recover before continuing.** After a confirmed reboot, wait for every node in the
   wave to return, verify the README's recovery conditions, and wait for HA to settle.
   Stop on any failed pre-flight or recovery check; do not continue to the next wave.
   This outranks finishing the task.
5. **Refuse to start** inside the 02:00 or 08:00 maintenance windows — alert suppression
   there would hide problems a reboot causes.

`riven` runs on `bray` and hosts both the agent session and the shared SSH agent. Before
asking for bray's final-wave confirmation, state that it terminates this session and
empties the agent. After the confirmed reboot command, stop: the human starts a new
session to verify bray. `clovis` runs the monitoring stack, so the blind window during
its reboot is expected, not an incident.

Report per node: whether a reboot is pending, the running vs installed kernel, and the
verification result. If nothing was pending, say so rather than implying work was done —
that is the expected outcome most weeks.

## Module Retirement

- Confirm it deploys nowhere first: no `<module>:` feature blocks in `hosts.conf`, and
  `./validate`'s orphan-module check clean afterward (it warns while a registered module
  has zero hosts enabling it).
- Prefer archiving over deleting when there's meaningful implementation history; trivial
  or obsolete modules can be `git rm`'d outright — precedent both ways in git log:
  `1639d7a` archives, `f5dbb5f`/`0e492c2` delete.
- `git mv` into `archive/retired-modules/` to preserve history: the orchestrator ->
  `.../src/homelab/modules/`; the module's top-level dir (`scripts/`, `templates/`,
  `configs/`, `README.md`, but not gitignored `build/`) -> `.../top-level/<module-dir>/`;
  dedicated tests -> `.../tests/` or `.../reference/`, renamed out of the `test_*.py`
  pattern so pytest stops collecting them.
- Deregister from `src/homelab/modules/__init__.py` (import, `MODULES`, `MODULE_ORDER`),
  the README module list, and any `hosts.py` schema validation.
- Run `./validate` afterward: the orphan warning clears and nothing else references it.

## Coding Style

- Python 3.13; local orchestration should be Python.
- Reuse `HostRegistry`, `HostConnection`, `DeploySession`, and the helpers in
  `src/homelab/module_support.py`; don't invent parallel deployment frameworks.
- Remote installers should source staged `lib/utils.sh` when present and reuse shared
  file-map helpers instead of reimplementing them.
- Bash: portable where shared, quote variables, `$(...)`, localized ShellCheck
  suppressions.
- `hosts.conf`: prefer full systemd calendar expressions (`*-*-* HH:MM:SS`) for
  schedule-like fields.
- No backup, disabled, or timestamped copies inside active config include directories such
  as `/etc/apt/apt.conf.d/`; use `/var/backups/homelab/<module>/` or remove superseded
  files.

Module shape, helper APIs, module-boundary decisions, SSH staging, logging, and ShellCheck
examples: the `deploy-module` skill, versioned in-repo at `.opencode/skill/deploy-module/`
alongside the code it describes.

## Inventory and Secrets

- `hosts.conf` is canonical inventory for real managed hosts, not a place to model
  convenience SSH aliases. Keep per-host connection metadata under `config` (`hostname`,
  `user`, `sshkey`, optional `agent`).
- Never commit `.env`, `telegram.env`, rendered secret files, tokens, passwords, or actual
  secret values. `.env.example` and `*.env.tpl.example` placeholders are allowed for
  offline validation.
- Rendered secrets live only in tmpfs under `/dev/shm` — never under the repo or a module
  `build/` directory.
- 1Password `op inject`, tmpfs staging/cache, bootstrap/purge, and runtime `.env`
  boundaries: the `homelab-secrets` skill.

## Before Handing Off

1. Read the module's orchestrator and `scripts/install.sh` before editing; match nearby
   patterns rather than introducing new framework styles.
2. Run targeted checks first (ruff/shellcheck + module dry-run), then `./validate`.
3. After pushing, verify the matching Actions run and read the error logs before
   considering the work complete.
4. Never commit secret files or generated `build/` artifacts.
