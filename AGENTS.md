# Homelab Agent Guide (AGENTS.md)

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
- For topology, VLANs, storage layout, heavy-path warnings, and cross-host context, read the Obsidian `Infrastructure Overview.md`.
- For module code changes, read the module orchestrator in `src/homelab/modules/` and the matching `<module>/scripts/install.sh` before editing.
- For Docker app placement and compose definitions, use the repo copy under `docker/`; do not inspect live `/mnt/cache/appdata` unless an explicit operational task requires a known app path.
- For unknown infrastructure paths, ask or search the Homelab Obsidian docs; do not discover paths by crawling live filesystems.

## Search and Scan Boundaries
- Do not run broad recursive scans on any homelab host under `/`, `/mnt/*`, `/mnt/cache`, `/mnt/tank`, `/vm-flash`, `/backup`, or `/srv/timemachine`.
- Do not adapt repo-local commands like `find .` to remote storage, media, backup, or appdata paths.
- Prefer repo-local `Glob`/`Grep`, `hosts.conf`, Infrastructure Overview, and linked guides before SSHing or searching remote hosts.
- If a path is not explicitly known from inventory or documentation, ask before scanning.

## Build, Lint, and Test Commands

### Full validation (best pre-PR check)
```bash
./validate
```
Includes Python compile checks, ShellCheck, `hosts.conf` parse validation, and module dry-runs.

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
The `tests/` directory covers host parsing, CLI validation, SSH helpers, build/template behavior, and module fallbacks. Add or update tests when changing those areas.

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

### Module-specific check scripts
```bash
./apcupsd/scripts/test-shutdown.sh
```
If a module has no dedicated test script, use `./deploy --dry-run` as the test.

## Layout and Runtime Pattern
- `src/homelab/modules/*.py`: local orchestrator for one module.
- `*/scripts/install.sh`: remote installer for staged bundle.
- `*/templates`: rendered files with Jinja `{{ VAR }}` placeholders when templated.
- `*/configs`: static files copied directly.
- `*/build`: generated output (gitignored).
- `secrets/`: 1Password-backed deploy-time secret catalog/templates only; no plaintext `.env` files should live here.

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
- Python lint.
- ShellCheck (warning severity).
- YAML lint for `hosts.conf`.
- `homelab validate`.
Run `PYTHONPATH=src .venv/bin/python -m homelab.cli validate` locally for CI parity (or just `./validate`).
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
