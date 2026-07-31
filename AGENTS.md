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

`.opencode/command/ship.md` runs validate -> dry-run -> deploy -> verify -> commit -> push
autonomously. It is for **routine, incremental changes to modules that already deploy
successfully**, and is always invoked explicitly — deciding to deploy is a human decision.

Renames, retirements, and a module's first-ever deploy are fine to ship **if** the dry-run
diff is actually read (read it, don't skim it — with no prior baseline to diff against,
that output is the only review) and the deploy still goes canary-first.

**Not for:** multi-module dirty trees (module inference is unreliable — split the
unrelated change into its own commit first), incident response (you want a tight manual
feedback loop), or offsite hosts (`cinci`, `cottonwood`).

| Tier | Modules | Rule |
| --- | --- | --- |
| 1 routine | `apcupsd`, `metrics-exporters`, `pve-notifications`, `disk-spindown`, `apt-upgrade`, `pve-http-boot`, `ubuntu-setup`, `wsl-conf` | `/ship` defaults are fine; idempotent and restart-safe. |
| 2 stateful | `zfs-automation`, `pbs-client-backup`, `pve-backup`, `docker`, `pve-postinstall`, `pve-gpu-passthrough` | Always name a host; canary mandatory; never bare `all`. Failure interrupts replication, backups, or running containers. |
| 3 control-path | `ssh-config`, `keepalived`, `pve-interface-pinning`, `pve-realtek-r8152-dkms`, `pve-upgrade`, `pve-autoinstall`, `pve-zfs-*-patch`, `pve-lxc-pre-replication-patch` | **Never `/ship`.** Deploy manually with console access confirmed. Failure severs SSH, networking, or the VIP, and the pipeline cannot verify or recover a host it can no longer reach. |

**Escalation ladder — never skip a rung:** manual `./deploy --dry-run <module> <host>`,
reading the diff yourself -> `/ship <module> <one-host>` as canary, reading the quoted
evidence rather than "verified OK" -> `/ship <module> all`, Tier 1 only and only after a
clean canary.

A **host diverged** report is the highest-priority outcome: deploy succeeded but the
predicate failed, so the host matches neither git nor its pre-state. Resolve that before
touching anything else.

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
