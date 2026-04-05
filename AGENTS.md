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

### Single-file lint (fast targeted check)
```bash
python -m ruff check src/homelab/cli.py
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
- `secrets/`: local operator inputs.

## Coding Style Guidelines

### Imports and shared functions
- Reuse `src/homelab/hosts.py`, `src/homelab/ssh.py`, and `deploy.py` from Python modules.
- Remote installers should source staged `lib/utils.sh` when present.
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

### SSH and remote execution
- Do not hardcode host lists; derive from `hosts list --feature ...`.
- Keep per-host connection metadata in `hosts.conf` under `config` (`type`, `hostname`, `user`, `sshkey`, optional `agent`) instead of duplicating it in static config files.
- Stage module bundles in `/tmp/homelab-<module>/`.
- Preserve root-user checks where module logic requires root SSH sessions.

### Module boundaries
- Prefer a new module when the capability is independently deployable (`./deploy <module> <host>` makes sense on its own), has its own validation requirements, or has a distinct service/restart/reboot/rebuild boundary.
- Prefer a feature or subfeature when it only changes what a parent module renders or installs and shares the same install/reload lifecycle.
- Good module examples in this repo: `pve-gpu-passthrough`, `ssh-config`, `zfs-automation`, `pve-backup`.
- Good feature examples in this repo: `ubuntu-setup.wireguard`, `ubuntu-setup.samba`, `ubuntu-setup.network.pin_interface`, `docker.backup`, `zfs-automation.sanoid`, `zfs-automation.replication`.
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
- `.env.example` is allowed.
- Validate secret file existence before starting deployment.

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
Run `PYTHONPATH=src python -m homelab.cli validate` locally for CI parity.

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
4. Run `PYTHONPATH=src python -m homelab.cli validate` when practical before handing off.
5. Never commit secret files or generated `build/` artifacts.
