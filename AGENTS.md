# Agent Instructions for Homelab Repository

**Repository:** `git@github.com:freender/homelab.git`  
**Type:** Shell-based infrastructure automation for Proxmox homelab  
**Language:** Bash/Shell  
**Hosts:** Proxmox cluster (ace, bray, clovis), PBS (xur), VMs

## Build, Lint, Test

No formal build system or automated tests. Validation is manual.

```bash
cd <module> && ./deploy.sh <host>       # Single module validation (preferred)
bash -x <module>/deploy.sh <host>       # Debug a deploy script
apcupsd/scripts/test-shutdown.sh        # Test script (UPS-specific)
```

**Post-deploy verification:**
```bash
ssh <host> "systemctl is-active --quiet <service>"  # Check running
ssh <host> "systemctl status <service>"              # Full status
ssh <host> "journalctl -u <service> -n 50"           # Recent logs
```

## Deployment Commands

```bash
./deploy-all.sh                         # All modules, all hosts
./deploy-all.sh <hostname>              # All modules, single host
cd <module> && ./deploy.sh all          # Single module, all hosts
cd <module> && ./deploy.sh <host>       # Single module, single host
cd <module> && ./deploy.sh --dry-run <host>  # Preview without deploying
cd <module> && ./remove.sh <host>       # Remove module from host
cd <module> && ./remove.sh --yes all    # Remove without confirmation
```

## Repository Layout

```
homelab/
├── deploy-all.sh           # Orchestrates all modules
├── lib/common.sh           # Shared functions (MUST source)
└── <module>/
    ├── deploy.sh           # Required entry point
    ├── remove.sh           # Optional removal script
    ├── hosts.conf          # Module host registry (YAML-like)
    ├── templates/          # Templated inputs (.tpl)
    ├── configs/            # Static configs and shared files
    ├── scripts/            # Install/remove helpers
    └── build/              # Generated artifacts (gitignored)
```

## deploy.sh Template

```bash
#!/bin/bash
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$SCRIPT_DIR/hosts.conf"
BUILD_ROOT="$SCRIPT_DIR/build"

# --- Host Selection ---
ARGS=$(parse_common_flags "$@")
set -- $ARGS

SUPPORTED_HOSTS=($(hosts list --type pve))
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping <module> (not applicable to $1)"
    exit 0
fi

# --- Validation ---
validate() {
    [[ ! -f "$SCRIPT_DIR/config.conf" ]] && { echo "Error: config.conf missing"; return 1; }
}
validate || exit 1

# --- Per-Host Deployment ---
deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"

    prepare_build_dir "$build_dir"
    cp "$SCRIPT_DIR/config.conf" "$build_dir/config.conf"

    # If using templates:
    # render_template "$SCRIPT_DIR/templates/config.tpl" "$build_dir/config" VAR1="value1" VAR2="value2"

    show_build_diff "$build_dir"

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-<module>/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-<module> && mkdir -p /tmp/homelab-<module>/build"
    scp -rq "$build_dir" "$host:/tmp/homelab-<module>/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-<module>/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-<module> && chmod +x scripts/install.sh && sudo ./scripts/install.sh $host"
}

# --- Main ---
deploy_init "<Module Name>"
deploy_run deploy $HOSTS
deploy_finish
```

## remove.sh Pattern

- Source common.sh, parse `--yes`/`-y` flag to skip confirmation
- Filter hosts with `filter_hosts`, use `SUPPORTED_HOSTS` array
- Track failures in `FAILED_HOSTS=()`, exit 1 if any failures
- Always backup before removing: `cp -r /etc/foo /etc/foo.bak.$(date +%Y%m%d%H%M%S)`

## lib/common.sh Functions

**hosts command** (unified host query interface):
- `hosts list` - all hosts
- `hosts list --type pve` - filter by type (pve, pbs, vm)
- `hosts list --feature telegraf` - filter by feature
- `hosts get <host> <key> [default]` - get property value
- `hosts has <host> <feature>` - check if host has feature (boolean)

**Deployment framework**:
- `deploy_init "Module Name"` - Initialize deployment (sets module name, clears failed hosts)
- `deploy_run <function> $HOSTS` - Run deployment function for each host with automatic failure tracking
- `deploy_finish` - Print summary, exit with appropriate code
- `parse_common_flags "$@"` - Parse --dry-run/-n flag, returns non-flag args

**Template rendering**:
- `render_template TEMPLATE OUTPUT VAR1=val1 VAR2=val2 ...` - Render template with variable substitution, auto-validates no unreplaced placeholders

**Build directory helpers**:
- `prepare_build_dir DIR` - Clean and create build dir, preserve previous as DIR.prev for diffing
- `show_build_diff DIR` - Show diff between current and previous build

**Other functions**:
- `filter_hosts ARG HOSTS...` - Filter CLI arg against supported hosts
- `deploy_file SRC HOST DEST [MODE] [OWNER]` - SCP + chmod/chown
- `deploy_script SRC HOST DEST` - Deploy executable (mode 755)
- `ensure_remote_dir HOST DIR [MODE]` - mkdir -p on remote
- `enable_service HOST SERVICE` - systemctl enable --now
- `verify_service HOST SERVICE` - Check service status, show logs if failed
- `print_header`, `print_action`, `print_sub`, `print_ok`, `print_warn` - Output helpers

**Global flags**:
- `$DRY_RUN` - Boolean, set by parse_common_flags when --dry-run/-n is passed

## hosts.conf Format

```yaml
ace:
  type: pve
  features:
    - telegraf
    - gpu

bray:
  type: pve
  ups.role: master
  features:
    - telegraf
    - ups-master
```

Access with: `hosts get ace ups.role`, `hosts has bray ups-master`

## Code Style

### Formatting
- 4-space indentation
- `[[ ... ]]` for conditionals (not `[ ]`)
- Quote all variables: `"${VAR}"`, `"$host"`
- `$(...)` over backticks
- Single-line SSH when possible

### Naming
- Constants: `UPPERCASE_WITH_UNDERSCORES`
- Variables: `lowercase` or `snake_case`
- Functions: `snake_case`
- Scripts: `lowercase-with-dashes.sh`

### Error Handling
- `set -e` inherited from common.sh (required)
- `set -u` in scripts accepting parameters
- Validate inputs early, exit 1 on failure
- `|| true` only for intentionally non-fatal commands
- Removal scripts: continue on errors, summarize failures at end

### SSH Commands
```bash
ssh "$host" "systemctl restart nginx"                       # Single command
ssh "$host" "apt-get update && apt-get install -y nginx"    # Chained commands

ssh "$host" bash <<'EOF'    # Heredoc for complex operations
systemctl stop service
rm -rf /tmp/cache
systemctl start service
EOF
```

### Config Files
- Heredocs for configs (no interactive editors)
- Quote delimiter to prevent expansion: `<<'EOF'`
- Templates in `configs/` directory

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
