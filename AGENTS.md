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
Test coverage detail (golden renders, pause semantics, network-critical modules) and
module-specific check scripts are documented in the `deploy-module` skill. Add or update
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

For module shape, helper APIs, module-boundary decisions, SSH staging, logging, ShellCheck examples, and targeted validation workflow, load the `deploy-module` skill.

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
