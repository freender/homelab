# Homelab Agent Guide (AGENTS.md)

## Purpose
This repository is Bash-first automation for a Proxmox homelab.
Use this file to make coding agents consistent with existing patterns.

## Repo Summary
- Languages: Bash + YAML.
- Inventory: `hosts.conf` (access through helper functions, not ad-hoc parsing).
- Shared libs: `lib/common.sh`, `lib/utils.sh`, `lib/print.sh`.
- Pattern: per-module `deploy.sh` stages files, then runs remote `scripts/install.sh`.
- CI: `.github/workflows/validate.yml`.

## Build, Lint, and Test Commands

### Full validation (best pre-PR check)
```bash
./validate.sh
```
Includes ShellCheck, `hosts.conf` parse validation, and module dry-runs.

### Lint all scripts + YAML
```bash
find . -name '*.sh' -not -path './.bin/*' -exec shellcheck -S warning {} +
yq eval '.' hosts.conf >/dev/null
```

### Single-file lint (fast targeted check)
```bash
shellcheck -S warning lib/common.sh
shellcheck -S warning pve-postinstall/deploy.sh
```

### Single module dry-run (primary "single test")
```bash
cd apcupsd && ./deploy.sh --dry-run ace
cd pve-postinstall && ./deploy.sh --dry-run all
```

### Full dry-run
```bash
./deploy-all.sh --dry-run all
```

### Module-specific check scripts
```bash
./apcupsd/scripts/test-shutdown.sh
```
If a module has no dedicated test script, use `deploy.sh --dry-run` as the test.

## Layout and Runtime Pattern
- `*/deploy.sh`: local orchestrator for one module.
- `*/scripts/install.sh`: remote installer for staged bundle.
- `*/templates`: rendered files with `${VAR}` placeholders.
- `*/configs`: static files copied directly.
- `*/build`: generated output (gitignored).
- `secrets/`: local operator inputs.

## Coding Style Guidelines

### Imports and shared functions
- In every module `deploy.sh`, source `lib/common.sh` first:
  `source "$(dirname "$0")/../lib/common.sh"`
- Remote installers should source staged `lib/utils.sh` when present.
- Keep fallback helper functions only where remote context may lack `utils.sh`.

### Formatting and syntax
- Shebang: `#!/bin/bash`.
- Indentation: 4 spaces, no tabs.
- Tests: prefer `[[ ... ]]`.
- Always quote variables unless deliberate splitting is required.
- Use `$(...)` command substitution, never backticks.
- Use `local` for function-scoped variables.

### Strict mode
- Existing baseline is `set -e` in `common.sh` and most installers.
- Use `set -u` only where pattern already exists (for example `deploy-all.sh`).
- Use `set -euo pipefail` only in scripts that already rely on it.
- Guard expected non-zero commands (for example `diff`) with `|| true`.

### Types and data handling (Bash)
- Treat booleans as `true`/`false` strings.
- Use arrays for lists (`local -a files`).
- Use arithmetic loops for numeric iteration.
- Validate `yq` output for empty/null before use.
- Prefer helper APIs over direct YAML scraping:
  - `hosts list [--feature <feature>]`
  - `hosts get <host> <key> [default]`
  - `hosts has <host> <feature>`

### Naming conventions
- Globals/constants: `UPPER_SNAKE_CASE` (`BUILD_ROOT`, `FORCE_UPDATE`).
- Locals/functions: `snake_case`.
- Module directories: kebab-case (`pve-gpu-passthrough`).
- Function names should describe action (`render_template`, `deploy_finish`).

### Deploy script shape
Keep the same flow used across modules:
1. Source common library and define paths.
2. Parse common flags (`parse_common_flags`) and module-specific flags.
3. Resolve supported hosts and filter target (`filter_hosts`).
4. Validate required templates/configs/secrets/host fields.
5. Implement `deploy()` per host:
   - prepare/render build artifacts
   - show remote diffs
   - obey dry-run mode
   - stage files to `/tmp/homelab-<module>/`
   - run remote `scripts/install.sh`
6. Finish with `deploy_init`, `deploy_run`, `deploy_finish`.

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
- Stage module bundles in `/tmp/homelab-<module>/`.
- Preserve root-user checks where module logic requires root SSH sessions.

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
- ShellCheck (warning severity).
- YAML lint for `hosts.conf`.
- Module dry-runs.
Run `./validate.sh` locally for CI parity.

## Cursor/Copilot Rules
Checked paths:
- `.cursor/rules/`
- `.cursorrules`
- `.github/copilot-instructions.md`
No Cursor/Copilot-specific rule files currently exist in this repo.
If they are added later, follow them and update this guide.

## Agent Checklist
1. Read the module's `deploy.sh` and `scripts/install.sh` before editing.
2. Match nearby patterns; avoid introducing new framework styles.
3. Run targeted validation first (single-file shellcheck + module dry-run).
4. Run `./validate.sh` when practical before handing off.
5. Never commit secret files or generated `build/` artifacts.
