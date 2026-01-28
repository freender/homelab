# Agent Instructions for Homelab Repository
Repository: `git@github.com:freender/homelab.git`
Type: Shell-based infrastructure automation for Proxmox homelab
Language: Bash/Shell
Hosts: Proxmox cluster (ace, bray, clovis), PBS (xur), VMs

## Build, Lint, Test
**Primary Validation (Run this first):**
```bash
./validate.sh                           # Runs shellcheck, yaml lint, and dry-runs all modules
```

**Single Module/Host Test (Dry Run):**
```bash
cd <module> && ./deploy.sh --dry-run <host>
```

**Debugging:**
```bash
bash -x <module>/deploy.sh <host>       # Debug execution trace
apcupsd/scripts/test-shutdown.sh        # UPS-specific test
```

**Post-Deploy Verification:**
```bash
ssh <host> "systemctl is-active --quiet <service>"
ssh <host> "systemctl status <service>"
ssh <host> "journalctl -u <service> -n 50"
```

## Deployment Commands
```bash
./deploy-all.sh                         # All modules, all hosts
./deploy-all.sh <hostname>              # All modules, single host
cd <module> && ./deploy.sh all          # Single module, all hosts
cd <module> && ./deploy.sh <host>       # Single module, single host
cd <module> && ./remove.sh <host>       # Remove module from host
cd <module> && ./remove.sh --yes all    # Remove without confirmation
```

## Repository Layout
```
homelab/
├── deploy-all.sh
├── validate.sh       # CI/Lint script
├── lib/
└── <module>/
    ├── deploy.sh
    ├── remove.sh
    ├── hosts.conf
    ├── templates/
    ├── configs/
    ├── scripts/
    └── build/        # gitignored
```

## lib/ Structure
`lib/common.sh`: Orchestration framework (hosts, flags, templates). Sources `utils.sh`.
`lib/utils.sh`: Remote-safe utilities. Sources `print.sh`.
`lib/print.sh`: Output helpers (zero dependencies).

## Key Patterns
**deploy.sh:**
- Source `lib/common.sh`
- Parse flags: `parse_common_flags "$@"` & `filter_hosts`
- Stage `/tmp/homelab-<module>/lib` + `print.sh`/`utils.sh`
- Use `render_template` (env subst) and `show_build_diff`

**remove.sh:**
- Source `lib/common.sh`
- Parse `--yes`
- Always `backup_config` before removal
- Continue on error (`FAILED_HOSTS` array)

## hosts.conf Format
Central inventory. Keys drive module inclusion.
```yaml
ace:
  type: pve
  telegraf:     # Enables telegraf module
  apcupsd:      # Enables apcupsd module
    role: slave
```
*Note: `zfs` monitoring is automatic for `type: pve|pbs`, no explicit key needed.*

## Code Style
**Formatting:**
- 4-space indentation
- `[[ ... ]]` for conditionals
- Quote ALL variables: `"${VAR}"`, `"$host"`
- `$(...)` over backticks

**Structure:**
- Scripts must start with `#!/bin/bash`
- `deploy.sh` must source `lib/common.sh`
- Keep logic in functions (`deploy`, `render_configs`)

**Naming:**
- Constants: `UPPERCASE_WITH_UNDERSCORES`
- Variables: `lowercase` or `snake_case`
- Functions: `snake_case`

**Error Handling:**
- `set -e` is inherited from `common.sh` (STRICT mode)
- Validate dependencies (files/vars) early
- `|| true` only for intentionally optional commands

**SSH & Configs:**
- Use heredocs for remote file writing (no interactive editors)
- Quote heredoc delimiters to prevent local expansion: `ssh "$host" bash <<'EOF'`
- Use `render_template` for local config generation

## Secrets
- Store in `.env` or `telegram.env` (gitignored)
- Commit `.env.example`
- Check existence: `[[ ! -f ".env" ]] && exit 1`

## Git Workflow
- Branch: `main`
- Commits: `type: message` (e.g., `fix: deploy script permission`, `feat: add new module`)
- Focus on "why" in descriptions
