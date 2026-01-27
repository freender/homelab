# Agent Instructions for Homelab Repository
Repository: `git@github.com:freender/homelab.git`
Type: Shell-based infrastructure automation for Proxmox homelab
Language: Bash/Shell
Hosts: Proxmox cluster (ace, bray, clovis), PBS (xur), VMs

## Build, Lint, Test
No formal build or linting. Validation is manual and per-module.
```bash
cd <module> && ./deploy.sh <host>             # Single module validation
cd <module> && ./deploy.sh --dry-run <host>
bash -x <module>/deploy.sh <host>             # Debug a deploy script
apcupsd/scripts/test-shutdown.sh              # UPS-specific test
```
Post-deploy verification:
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
├── lib/
└── <module>/
    ├── deploy.sh
    ├── remove.sh
    ├── hosts.conf
    ├── templates/
    ├── configs/
    ├── scripts/
    └── build/            # gitignored
```

## lib/ Structure
```
lib/
├── print.sh      # Output helpers (zero dependencies)
├── utils.sh      # Remote-safe utilities (sources print.sh)
└── common.sh     # Full orchestration framework (sources utils.sh)
```
print.sh
- `print_header`, `print_action`, `print_sub`, `print_ok`, `print_warn`
utils.sh (safe for remote hosts)
- `backup_config PATH` - Backup file/dir to PATH.bak.YYYYMMDDHHmmss
common.sh (helm/orchestration)
- `hosts list|get|has` (requires yq)
- `filter_hosts`, `parse_common_flags`, `render_template`
- `prepare_build_dir`, `show_build_diff`
- `deploy_init`, `deploy_run`, `deploy_finish`

## Key Patterns
deploy.sh
- Source `lib/common.sh`
- Parse flags with `parse_common_flags` and use `filter_hosts`
- Stage `/tmp/homelab-<module>/lib` and scp `lib/print.sh` + `lib/utils.sh`
- Use `render_template` for templates and `show_build_diff` for build diffs
remove.sh
- Source `lib/common.sh`
- Parse `--yes`/`-y` to skip confirmation
- Track failures in `FAILED_HOSTS=()` and exit 1 if any
- Always call `backup_config` before removing configs
- Stage `lib/utils.sh` on remote and source via heredoc

## hosts.conf Format
```yaml
ace:
  type: pve
  telegraf:
  pve-gpu-passthrough:
```
Access with: `hosts get ace apcupsd.role`, `hosts has bray apcupsd`

## Code Style
Formatting
- 4-space indentation
- `[[ ... ]]` for conditionals (not `[ ]`)
- Quote variables: "${VAR}", "$host"
- `$(...)` over backticks
Imports and structure
- `deploy.sh` and `remove.sh` must source `lib/common.sh`
- `scripts/install.sh` should source `lib/utils.sh` with a fallback
- Keep host selection, validation, and deploy steps in distinct sections
Naming
- Constants: `UPPERCASE_WITH_UNDERSCORES`
- Variables: `lowercase` or `snake_case`
- Functions: `snake_case`
- Scripts: `lowercase-with-dashes.sh`
Error handling
- `set -e` inherited from `common.sh` (required)
- `set -u` in scripts accepting parameters
- Validate inputs early and exit 1 on failure
- `|| true` only for intentionally non-fatal commands
- Removal scripts continue per-host and summarize failures
SSH patterns
```bash
ssh "$host" "systemctl restart nginx"
ssh "$host" "apt-get update && apt-get install -y nginx"
ssh "$host" bash <<'EOF'
systemctl stop service
rm -rf /tmp/cache
systemctl start service
EOF
```
Config files and templates
- Use heredocs for configs (no interactive editors)
- Quote delimiters: `<<'EOF'`
- Templates live in `templates/` and use `render_template`
Docker ownership overrides
- Optional per-host overrides: `docker.owner`, `docker.group`
- Defaults to `docker.user` if not set

## Secrets
- Store in `.env` or `telegram.env` (gitignored)
- Provide `.env.example` templates
- Never commit secrets
- Validate: `[[ ! -f "$SCRIPT_DIR/.env" ]] && echo "Error: Missing .env" && exit 1`

## Git Workflow
- Work on `main` branch
- Commit prefixes: `add`, `update`, `fix`, `refactor`, `docs`
- Focus on why, not what

## Cursor/Copilot Rules
No `.cursor/rules/`, `.cursorrules`, or `.github/copilot-instructions.md` found.
