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

### Imports and shared functions
- Reuse `src/homelab/hosts.py`, `src/homelab/ssh.py`, and `deploy.py` from Python modules.
- Reuse `src/homelab/module_support.py` for shared file-map module helpers like `FileSpec`, `HostArtifacts`, `require_text`, `write_file_map`, `tmpfs_secret_stage`, and `simple_root_installer_deploy`.
- Remote installers should source staged `lib/utils.sh` when present.
- Remote installers using `file-map.conf` should use shared `load_file_map`, `mapped_dest`, `mapped_mode`, `install_build_file`, and `install_file_map` helpers from `lib/utils.sh` instead of reimplementing them per module.
- Keep fallback helper functions only where remote context may lack `utils.sh`.

### Formatting and syntax
- Python target: 3.13.
- Local orchestration should be Python.
- Remote Bash should stay compatible with the older macOS Bash 3.x baseline when shared locally, though most remote scripts are Linux-only.
- Indentation: 4 spaces, no tabs.
- Tests: prefer `[[ ... ]]`.
- Always quote variables unless deliberate splitting is required.
- Use `$(...)` command substitution, never backticks.
- Use `local` for function-scoped variables.
- Avoid Bash 4+ features in shared/deploy scripts unless the script is explicitly Linux-only.

### Strict mode
- Python should raise on hard failures and return `0` for not-applicable module skips.
- Remote Bash strict mode should match nearby patterns.
- Guard expected non-zero commands (for example `diff`) with `|| true` in Bash.

### Types and data handling
- Prefer `HostRegistry` helpers over ad-hoc YAML traversal.
- Treat booleans passed into remote env files as `true`/`false` strings where existing installers expect them.

### Schedule syntax
- In `hosts.conf`, prefer full calendar expressions for schedule-like fields instead of shorthand clock strings.
- Use `*-*-* HH:MM:SS` for daily times and richer expressions like `Mon..Sat *-*-* 00:00:00` when needed.
- Avoid introducing new shorthand values like `00:30`, `09:00`, or `daily` in inventory unless a consumer explicitly requires a different syntax.

### Naming conventions
- Globals/constants: `UPPER_SNAKE_CASE` (`BUILD_ROOT`, `FORCE_UPDATE`).
- Locals/functions: `snake_case`.
- Module directories: kebab-case (`pve-gpu-passthrough`).
- Function names should describe action (`render_template`, `deploy_finish`).

### Deploy module shape
Keep the same flow used across modules:
1. Resolve supported hosts via `hosts.conf`.
2. Validate required templates/configs/secrets/host fields.
3. Implement `deploy_host()` per host:
   - prepare/render build artifacts
   - show remote diffs
   - obey dry-run mode
   - stage files to `/tmp/homelab-<module>/`
   - run remote `scripts/install.sh`
4. Use `DeploySession` for host-level success/failure reporting.

### Logging/output
- Prefer `print_header`, `print_action`, `print_sub`, `print_ok`, `print_warn`, `print_error`.
- Keep output operational and short.
- Use warnings for recoverable conditions; exit non-zero on hard failures.

### Error handling and idempotency
- Fail fast on missing required files/secrets/config keys.
- Return `0` for "not applicable" module/host skips.
- Track host-level failures via deployment framework arrays.
- Copy/update only when content changes unless `FORCE_UPDATE=true`.
- Do not leave backup, disabled, or timestamped copies inside active config include directories such as `/etc/apt/apt.conf.d/`; apt treats invalid extensions there as notices. Store backups under `/var/backups/homelab/<module>/` or remove superseded files.

### SSH and remote execution
- Do not hardcode host lists; derive from `hosts list --feature ...`.
- Keep per-host connection metadata in `hosts.conf` under `config` (`type`, `hostname`, `user`, `sshkey`, optional `agent`) instead of duplicating it in static config files.
- Stage module bundles in `/tmp/homelab-<module>/`.
- Preserve root-user checks where module logic requires root SSH sessions.
- For offsite hosts (`cottonwood-root`, `cinci-root`), first check whether a direct 1Password SSH Agent identity named `1Password SSH Key - Offsite` is already loaded/available in the shared agent and use it if present. If unavailable, do not auto-load access; ask the user to intentionally run `addoffsitekey` only when they want offsite access available.

### Module boundaries
- Prefer a new module when the capability is independently deployable (`./deploy <module> <host>` makes sense on its own), has its own validation requirements, or has a distinct service/restart/reboot/rebuild boundary.
- Prefer a feature or subfeature when it only changes what a parent module renders or installs and shares the same install/reload lifecycle.
- Good module examples in this repo: `pve-gpu-passthrough`, `ssh-config`, `zfs-automation`, `pve-backup`.
- Good feature examples in this repo: `ubuntu-setup.wireguard`, `ubuntu-setup.samba`, `ubuntu-setup.network.pin_interface`, `docker.update_schedule`, `zfs-automation.sanoid`, `zfs-automation.replication`.
- If a concern owns packages, systemd units, generated config, and its own rebuild behavior, it usually deserves a module.
- If a concern is just policy/data for a parent module, keep it inside the parent instead of creating a thin wrapper module.

### Inventory and aliases
- Treat `hosts.conf` as canonical inventory for real managed hosts, not as a place to model convenience SSH aliases.
- Keep real per-host connection metadata under `config` (`hostname`, `user`, `sshkey`, optional `agent`).
- Prefer expressing alternate SSH behaviors as SSH config aliases or as module/framework connection overrides, rather than adding duplicate inventory entries like `foo-root` for the same machine.
- Only add a second inventory entry for the same machine when it is meaningfully managed as a separate target with different features or lifecycle; avoid alias-only inventory duplication.
- Long term, favor one inventory host per real machine and keep alias/routing concerns in SSH config or connection logic.

### Secrets and sensitive data
- Never commit `.env`, `telegram.env`, or actual secret values.
- Homelab repo deploy-time secrets are sourced from 1Password via `src/homelab/op_secrets.py` and `op inject`.
- `secrets/catalog.yml` maps stable secret names to `secrets/templates/*.env.tpl` files containing `op://Homelab/<Item>/<field>` references.
- Rendered secrets must live only in tmpfs paths under `/dev/shm`, including the 24-hour cache at `/dev/shm/homelab-secret-cache-$UID` and per-run staging dirs like `/dev/shm/homelab-secrets.*`; do not write generated secret files under the repo.
- `op_secrets.secret_file()` has a 24-hour tmpfs cache by default to reduce 1Password service-account reads. Use `HOMELAB_SECRET_CACHE_TTL=0` to disable it, `homelab secrets cache-status` to inspect it, and `homelab secrets cache-clear` to shred/remove it early.
- When a module needs to stage rendered secrets into a remote bundle, use `tmpfs_secret_stage()`/`copy_cached_secret()` and upload the temporary file; do not copy cached secret files into module `build/` directories.
- The normal read-only service-account token path on `riven` is `~/.config/op/service-account-token`; `~/.config/op/homelab.token` is reserved for a temporary bootstrap token and should not remain after bootstrap.
- Use `PATH="$HOME/.local/bin:$PATH" PYTHONPATH=src .venv/bin/python -m homelab.cli secrets doctor` to verify 1Password secret resolution.
- Use `homelab secrets bootstrap` only for one-time migration from existing ignored `secrets/*.env` files into 1Password; it requires temporary write access and should be followed by `homelab secrets purge-local` and token downgrade/removal.
- `.env.example` and `*.env.tpl.example` placeholders are allowed for offline validation.
- Keep Docker compose `.env` files out of this repo secret workflow; compose runtime env files stay on the Docker hosts under `/mnt/cache/appdata/<app>/`.
- Keep real domain names, public route hosts, and externally reachable URLs out of committed inventory/templates unless already intentionally modeled as non-secret infrastructure metadata.

### ShellCheck directives
- Use suppressions only when necessary and localize them near the affected line.
- Typical accepted cases in this repo:
  - `SC1090` dynamic source path.
  - `SC2086` intentional splitting on controlled values.

## CI Expectations
CI on push/PR to `main` runs:
- Python lint.
- ShellCheck (warning severity).
- YAML lint for `hosts.conf`.
- `homelab validate`.
Run `PYTHONPATH=src .venv/bin/python -m homelab.cli validate` locally for CI parity (or just `./validate`).
After pushing, check the GitHub Actions run status for that push and inspect failures immediately if any job is red.

## Cursor/Copilot Rules
Checked paths:
- `.cursor/rules/`
- `.cursorrules`
- `.github/copilot-instructions.md`
No Cursor/Copilot-specific rule files currently exist in this repo.
If they are added later, follow them and update this guide.

## Agent Checklist
1. Read the module's Python orchestrator and `scripts/install.sh` before editing.
2. Match nearby patterns; avoid introducing new framework styles.
3. Run targeted validation first (ruff/shellcheck + module dry-run).
4. Run `PYTHONPATH=src .venv/bin/python -m homelab.cli validate` when practical before handing off (or just `./validate`).
5. After any push, verify the matching GitHub Actions run and review error logs before considering the work complete.
6. Never commit secret files or generated `build/` artifacts.
