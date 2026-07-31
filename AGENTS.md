# Homelab Agent Guide (AGENTS.md)

## Task type
- **Repo code/config change or `./deploy` invocation** (edit `src/homelab/`, add/modify a
  module, run `./validate`, or invoke `./deploy` itself — dry-run or live, single
  module/host or `all all`): focus on the Build/Test commands below and load the
  `deploy-module` skill. Topology is not needed; `./deploy` reads `hosts.conf` itself.
- **Live homelab operation** (SSH to inspect/debug a running host directly, cross-host
  investigation not covered by `./deploy`): load the `homelab-infra` skill for topology,
  then the relevant topic skill.

## Purpose
This repository is Python-orchestrated automation for a Proxmox homelab.
Use this file to make coding agents consistent with existing patterns.

## Public Repo Boundary

**`freender/homelab` is a public GitHub repo.** Everything committed is
world-readable and permanent, including history. Treat every edit as publication.

**Never commit:**
- The real public domain used for homelab service routes, or any externally
  reachable route host/URL. Do not spell that domain out anywhere in this repo —
  not in docs, comments, tests, or CI config. It currently appears zero times;
  keep it that way. Use `example.net` placeholders, as templates already do
  (`traefik-tower.example.net`).
- Secrets in any form: `.env`, rendered secret files, tokens, API keys, passwords,
  SSH private keys, PBS encryption keys **or their escrow locations**.
- Third-party detail for `cinci`/`cottonwood` beyond what inventory needs — physical
  location, LAN topology, or the owners' personal data. These are other people's
  networks.
- Live security posture: WireGuard/Pi-KVM route topology, Crowdsec/middleware
  allowlists, per-host SSH auth matrices, agent socket paths, credential file
  locations.

**Intentionally public — do NOT "sanitize" these:** `*.freender.internal` hostnames,
RFC1918 IPs, usernames, NIC MACs, SSH *public* keys, PBS account/token *names*, and
schedules. `hosts.conf` depends on them; scrubbing them breaks deploys.

`*.freender.internal` is **internal split-horizon DNS**, resolvable only on the LAN
and never from the internet. It is deploy metadata, not attack surface, and is safe
to commit — do not confuse it with the public route domain above, which is the one
that must never appear here. Same rule for RFC1918 addresses: unroutable, so they
carry no external exposure. The leak check treats `.internal` (and `.local`,
`.lan`, `.invalid`, `.test`) as internal TLDs and ignores them by design.

Vendor URLs (`github.com`, `download.proxmox.com`, `get.docker.com`,
`api.telegram.org`) are fine.

**Agent instructions and skills:** only repo-scoped tooling docs belong here (see
`.opencode/skill/deploy-module/`). Topology, storage, backup, SSH, offsite,
monitoring, and secret-handling skills stay host-local in
`~/.config/opencode/skills/` — they are credential and recon maps, not repo
documentation.

`./validate` enforces the domain/secret half of this mechanically (`leak check`); the
judgment calls above are still yours.

## Repo Summary
- Languages: Python + Bash + YAML.
- Inventory: `hosts.conf`.
- Local orchestration: `src/homelab/`.
- Shared remote libs: `lib/utils.sh`, `lib/print.sh`.
- Pattern: Python module builds/stages files, then runs remote `scripts/install.sh`.
- CI: `.github/workflows/validate.yml`.

## AI Quick Path
- For host facts, SSH metadata, deploy targets, and feature config, read `hosts.conf` first.
- For topology, VLANs, storage layout, heavy-path warnings, and cross-host context, load the `homelab-infra` skill (self-contained topology map).
- For module code changes, read the module orchestrator in `src/homelab/modules/` and the matching `<module>/scripts/install.sh` before editing.
- For Docker app placement and compose definitions, use the repo copy under `docker/`; do not inspect live `/mnt/cache/appdata` unless an explicit operational task requires a known app path.
- For unknown infrastructure paths, ask or search the Homelab Obsidian docs; do not discover paths by crawling live filesystems.

## Search and Scan Boundaries
- Do not run broad recursive scans on any homelab host under `/`, `/mnt/*`, `/mnt/cache`, `/mnt/tank`, `/vm-flash`, `/backup`, or `/srv/timemachine`.
- Do not adapt repo-local commands like `find .` to remote storage, media, backup, or appdata paths.
- Prefer repo-local `Glob`/`Grep`, `hosts.conf`, the `homelab-infra` skill, and linked guides before SSHing or searching remote hosts.
- If a path is not explicitly known from inventory or documentation, ask before scanning.

## Build, Lint, and Test Commands

### Full validation (best pre-PR check)
```bash
./validate
```
Runs every check CI gates on: Python compile, Ruff, Pytest, `hosts.conf` parse
validation, the inventory/module cross-check, ShellCheck, and module dry-runs. Ruff and
Pytest are skipped with a warning if they are not installed, so run it from the repo
`.venv` (or `uv run`) for true CI parity.

### Lint all scripts + YAML
```bash
find . -name '*.sh' -not -path './.bin/*' -exec shellcheck -S warning {} +
yq eval '.' hosts.conf >/dev/null
```
Run `find .` only from the repository root; never adapt this pattern to `/`, `/mnt`, or remote/live storage paths.

### Unit tests
```bash
.venv/bin/python -m pytest tests/
```
The `deploy-module` skill carries the test coverage map (golden renders, pause
semantics, network-critical modules) and which test owns which area. Add or update
tests when touching those areas.

### Single-file lint (fast targeted check)
```bash
.venv/bin/python -m ruff check src/homelab/cli.py
shellcheck -S warning pve-postinstall/scripts/install.sh
```

### Single module dry-run (primary "single test")
```bash
./deploy --dry-run apcupsd ace
./deploy --dry-run pve-postinstall all
```

### Full dry-run
```bash
./deploy --dry-run all all
```

## Layout and Runtime Pattern
- `src/homelab/modules/*.py`: local orchestrator for one module.
- `*/scripts/install.sh`: remote installer for staged bundle.
- `*/templates`: rendered files with Jinja `{{ VAR }}` placeholders when templated.
- `*/configs`: static files copied directly.
- `*/build`: generated output (gitignored).
- `secrets/`: 1Password-backed deploy-time secret catalog/templates only; no plaintext `.env` files should live here.

## Deploy, Disable, and Pause Patterns

Three distinct "off/freeze" switches in `hosts.conf` — do not conflate them:

- **`deploy: false`** (host-level feature gate; formerly `enabled: false`). Removes the
  host from the module's deploy targets: module skipped, running service **never
  touched**. `deploy` wins if both present; legacy `enabled: false` warns.
- **`<feature>.paused: true`** (module-wide pause). Keeps the feature deployed but
  **stops+disables its managed systemd units**; reversible. Supported by
  `disk-spindown`, `apt-upgrade`, `pbs-client-backup`, `zfs-automation`.
- **Per-job `paused: true`** (fine-grained). Pauses one unit while others run (e.g.
  `zfs-automation.replication_jobs.<job>.paused: true`). Distinct from the job's
  `enabled: false`, which retires it entirely (unit files removed).

For the implementation how-to (adding `paused` to a module: Python flag read, the
`homelab_apply_pause` bash helper, unit-file semantics), load the `deploy-module` skill.

## Shipping Strategy (`/ship`)

`/ship` (`.opencode/command/ship.md`) runs validate -> dry-run -> deploy -> verify ->
commit -> push autonomously. It is for **routine, incremental changes to modules that
already deploy successfully**. It is invoked explicitly and never triggered
automatically — deciding to deploy is a human decision.

**Renames and first-ever deploys are fine to `/ship`** as long as the dry-run diff
is actually reviewed before deploying (read it, don't skim it) and the deploy still
goes canary-first — a rename or a module's first deploy has no prior baseline to
diff against, so the dry-run output itself is the review.

**Do not use `/ship` for:** multi-module working trees (module inference is
unreliable — if the dirty tree mixes an unrelated change, split it into its own
commit before shipping); incident response (you want a tight manual feedback
loop); offsite hosts (`cinci`, `cottonwood`).

**Risk tiers — govern the host argument:**

| Tier | Modules | Rule |
| --- | --- | --- |
| 1 routine | `apcupsd`, `metrics-exporters`, `pve-notifications`, `disk-spindown`, `apt-upgrade`, `pve-http-boot`, `ubuntu-setup`, `wsl-conf` | `/ship` defaults are fine; idempotent and restart-safe. |
| 2 stateful | `zfs-automation`, `pbs-client-backup`, `pve-backup`, `docker`, `pve-postinstall`, `pve-gpu-passthrough` | Always name a host; canary mandatory; never bare `all`. Failure interrupts replication, backups, or running containers. |
| 3 control-path | `ssh-config`, `keepalived`, `pve-interface-pinning`, `pve-realtek-r8152-dkms`, `pve-upgrade`, `pve-autoinstall`, `pve-zfs-*-patch`, `pve-lxc-pre-replication-patch` | **Do not use `/ship`.** Deploy manually with console access confirmed. Failure severs SSH, networking, or the VIP, and the pipeline cannot verify or recover from a host it can no longer reach. |

**Escalation ladder — never skip a rung:**

1. `./deploy --dry-run <module> <host>` manually; read the diff yourself.
2. `/ship <module> <one-host>` as canary; read the quoted evidence, not "verified OK".
3. `/ship <module> all` — Tier 1 only, and only after step 2 was clean.

A **host diverged** report is the highest-priority outcome: deploy succeeded but the
predicate failed, so the host matches neither git nor its pre-state. Resolve that
before touching anything else.

## Module Retirement / Archival

- Before retiring a module, confirm it deploys nowhere: no `<module>:` feature blocks in
  `hosts.conf`, and `./validate`'s orphan-module check is clean afterward (it warns while
  a registered module has zero hosts enabling it).
- Prefer archiving over deleting when the module has meaningful implementation history;
  trivial/obsolete modules can be `git rm`'d outright (see git log precedent for both:
  `1639d7a` archives, `f5dbb5f`/`0e492c2` delete outright).
- Archive layout under `archive/retired-modules/` (use `git mv` to preserve history):
  - `src/homelab/modules/<module>.py` -> `archive/retired-modules/src/homelab/modules/`
  - the module's top-level dir (`scripts/`, `templates/`, `configs/`, `README.md`; not
    `build/`, which is gitignored) -> `archive/retired-modules/top-level/<module-dir>/`
  - any dedicated tests -> `archive/retired-modules/tests/` or `.../reference/`, renamed
    out of the `test_*.py` pattern so pytest stops collecting them.
- Remove the module from `src/homelab/modules/__init__.py` (import, `MODULES` entry,
  `MODULE_ORDER`), from the README module list, and from any `hosts.py` schema
  validation.
- Run `./validate` after retiring to confirm the orphan-module warning clears and nothing
  else references the removed module.

## Coding Style Guidelines

- Python target: 3.13; local orchestration should be Python.
- Reuse `HostRegistry`, `HostConnection`, `DeploySession`, and helpers in `src/homelab/module_support.py`; do not invent parallel deployment frameworks.
- Remote installers should source staged `lib/utils.sh` when present and reuse shared file-map helpers instead of reimplementing them.
- Keep Bash portable where scripts are shared, quote variables, use `$(...)`, and localize ShellCheck suppressions.
- In `hosts.conf`, prefer full systemd calendar expressions like `*-*-* HH:MM:SS` for schedule-like fields.
- Do not leave backup, disabled, or timestamped copies inside active config include directories such as `/etc/apt/apt.conf.d/`; use `/var/backups/homelab/<module>/` or remove superseded files.

For module shape, helper APIs, module-boundary decisions, SSH staging, logging, and ShellCheck examples, load the `deploy-module` skill. It lives in this repo at `.opencode/skill/deploy-module/` and is versioned alongside the code it describes.

## Inventory And Secrets

- Treat `hosts.conf` as canonical inventory for real managed hosts, not as a place to model convenience SSH aliases.
- Keep real per-host connection metadata under `config` (`hostname`, `user`, `sshkey`, optional `agent`).
- Never commit `.env`, `telegram.env`, rendered secret files, tokens, passwords, or actual secret values.
- Rendered secrets must live only in tmpfs paths under `/dev/shm`; do not write generated secret files under the repo or module `build/` directories.
- `.env.example` and `*.env.tpl.example` placeholders are allowed for offline validation.
- For deploy-time secrets, 1Password `op inject`, tmpfs staging/cache, bootstrap/purge, and runtime `.env` boundaries, load the `homelab-secrets` skill.

## CI Expectations
CI on push/PR to `main` runs:
- Python lint (Ruff).
- Pytest.
- ShellCheck (warning severity).
- YAML lint for `hosts.conf`.
- `homelab validate`.
Run `PYTHONPATH=src .venv/bin/python -m homelab.cli validate` locally for CI parity (or just `./validate`) — it runs Ruff and Pytest too.
After pushing, check the GitHub Actions run status for that push and inspect failures immediately if any job is red.

## Cursor/Copilot Rules
If Cursor/Copilot rule files are added later, follow them and update this guide.

## Agent Checklist
1. Read the module's Python orchestrator and `scripts/install.sh` before editing.
2. Match nearby patterns; avoid introducing new framework styles.
3. Run targeted validation first (ruff/shellcheck + module dry-run).
4. Run `PYTHONPATH=src .venv/bin/python -m homelab.cli validate` when practical before handing off (or just `./validate`).
5. After any push, verify the matching GitHub Actions run and review error logs before considering the work complete.
6. Never commit secret files or generated `build/` artifacts.
