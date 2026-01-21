# Agent Instructions for Homelab Repository

**Repository:** `git@github.com:freender/homelab.git`
**Type:** Shell-based infrastructure automation for Proxmox homelab
**Primary Language:** Bash/Shell
**Infrastructure:** 3-node Proxmox cluster (ace, bray, clovis), PBS (xur), VMs, remote NAS

## Build, Lint, Test

No formal build system, linter, or automated tests.

**Single-test / narrow validation:**
- `apcupsd/scripts/test-shutdown.sh` (run on the UPS host)
- Module deploy dry-run by targeting a single host: `cd <module> && ./deploy.sh <host>`

**Manual verification (typical):**
- Service status: `ssh <host> "systemctl status <service>"`
- Service active check: `ssh <host> "systemctl is-active --quiet <service>"`
- Logs: `ssh <host> "journalctl -u <service> -n 50"`

**Pragmatic checks when editing a module:**
1) Deploy only the module to one host.
2) Validate config file is present and service is active.
3) Review recent logs for errors.

## Deployment Commands

```bash
# Deploy all modules to all hosts
./deploy-all.sh

# Deploy all modules to a specific host
./deploy-all.sh <hostname>

# Deploy a single module to all supported hosts
cd <module> && ./deploy.sh all

# Deploy a single module to specific hosts
cd <module> && ./deploy.sh <host1> <host2>
```

## Repository Layout

```
homelab/
├── deploy-all.sh
├── lib/common.sh
└── <module>/
    ├── deploy.sh
    ├── hosts.conf      # Module-specific registry
    ├── configs/        # Optional
    ├── scripts/        # Optional
    └── README.md
```

## Script Structure (deploy.sh)

```bash
#!/bin/bash
source "$(dirname "$0")/../lib/common.sh"

SUPPORTED_HOSTS=($(get_hosts_by_type pve))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping <module> (not applicable to $1)"
    exit 0
fi
```

## Code Style Guidelines

### Imports and Structure
- Source shared helpers at the top of `deploy.sh` scripts.
- Cache script paths with `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"` when needed.
- Prefer `lib/common.sh` helpers: `filter_hosts`, `deploy_file`, `deploy_script`, `ensure_remote_dir`.

### Formatting
- Use 4-space indentation.
- Use `[[ ... ]]` for tests, and quote all variables: `"${VAR}"`.
- Keep SSH commands on single lines unless a heredoc is required.
- Prefer `$(...)` over backticks.

### Naming Conventions
- Constants and environment variables: `UPPERCASE_WITH_UNDERSCORES`.
- Local variables: `lowercase` or `lower_snake_case`.
- Functions: `lower_snake_case`.
- Scripts: `lowercase-with-dashes.sh`.

### Types and Validation
- Bash has no types; use explicit validation.
- Use `command -v <tool>` checks before installing dependencies.
- Guard missing config with clear error messages and `exit 1`.
- Validate required files before running `deploy_file` or `deploy_script`.

### Error Handling
- `set -e` is required in scripts that execute commands (inherit via `lib/common.sh`).
- `set -u` is recommended when scripts accept parameters.
- Validate required inputs early and exit with non-zero on failure.
- Use `|| true` only for intentionally non-fatal commands.
- Wrap risky remote actions with explicit checks and clear failure output.

### Host Filtering and Registry
- Use module-scoped `hosts.conf` as the source of truth for host types and features.
- `SUPPORTED_HOSTS` should come from helpers like `get_hosts_by_type`.
- Use `filter_hosts` and return `exit 0` for non-applicable hosts.

### Output and Logging
- Prefer `print_header`, `print_action`, `print_sub`, `print_ok`, `print_warn` from `lib/common.sh`.
- Keep output consistent for deploy-all aggregation.
- Log changes with explicit explains (what, where, why) before running remote commands.

### SSH and File Transfer
- Use `ssh "$HOST" "command"` for single commands.
- Use `deploy_file` / `deploy_script` for consistent permissions and ownership.
- Stage files under `/tmp/homelab-<module>` when doing multi-file installs.

### Config Files
- Use heredocs for configs; do not open interactive editors.
- Quote heredoc delimiter to avoid variable expansion when needed.
- Keep config templates in `configs/` when reused across hosts.

## Common Tasks

- Deploy one module for validation: `cd <module> && ./deploy.sh <host>`
- Debug a deploy script: `bash -x <module>/deploy.sh <host>`
- Check if a service is active: `ssh <host> "systemctl is-active --quiet <service>"`
- Tail logs while validating: `ssh <host> "journalctl -u <service> -f"`

## Documentation Style (Module README.md)

- Command-focused, minimal prose.
- Numbered steps `1) 2) 3)` with copy-paste-ready commands.
- Include `SCOPE`, `NODES`, and `REBUILD` metadata under `## Installation`.
- Use heredocs for config files.
- Add a `## Quick Reference` section with key commands or values.

## Secrets

- Store secrets in `.env` or `telegram.env` (gitignored).
- Provide `.env.example` templates for required values.
- Never commit secrets; validate existence before deployment.

## Git Workflow

- Work directly on `main` unless explicitly branching.
- Commit prefixes: `add`, `update`, `fix`, `refactor`, `docs`.
- Commit messages focus on why, not the file list.

## Cursor and Copilot Rules

- No `.cursor/rules/`, `.cursorrules`, or `.github/copilot-instructions.md` files found.

**Last updated:** 2026-01-21
