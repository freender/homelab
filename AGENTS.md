# Agent Instructions for Homelab Repository

## Project Overview
Shell-based infrastructure automation for a Proxmox homelab.
Modular Bash scripts deploy configurations to hosts defined in `hosts.conf`.

- **Language:** Bash (Shell)
- **Config:** YAML (`hosts.conf`) parsed by `yq`
- **Hosts:** Proxmox nodes (ace, bray, clovis, osiris), TrueNAS (tower), Ubuntu VMs, macOS
- **CI:** GitHub Actions runs ShellCheck, YAML lint, and dry-run on push/PR to `main`

---

## 1. Build, Lint, and Test Commands

### Full Validation (run before committing)
```bash
./validate.sh
```
Runs ShellCheck, YAML validation, and dry-run deployments for all modules.

### Linting
```bash
find . -name '*.sh' -not -path './.bin/*' -exec shellcheck -S warning {} +
```

### Single Module Dry Run (primary way to "test")
```bash
# Single host:
cd apcupsd && ./deploy.sh --dry-run ace

# All hosts for a module:
cd apcupsd && ./deploy.sh --dry-run all
```

### Full Dry Run
```bash
./deploy-all.sh --dry-run all
./deploy-all.sh --dry-run ace     # single host, all applicable modules
```

### Force Apply Managed Files
```bash
./deploy-all.sh --force all
cd <module> && ./deploy.sh --force <host>
```
Forces installers to rewrite managed files even when content is unchanged.

### Debugging
```bash
bash -x apcupsd/deploy.sh ace    # trace execution
```

---

## 2. Code Style & Conventions

### Bash Standards
- **Shebang:** `#!/bin/bash` (always first line)
- **Indentation:** 4 spaces, no tabs
- **Strict mode:** `lib/common.sh` sets `set -e`; remote `install.sh` scripts set `set -e` directly
- **Conditionals:** `[[ ... ]]` not `[ ... ]`
- **Quoting:** Always quote variables: `"$VAR"`, `"$host"`, `"${ARRAY[@]}"`
- **Command substitution:** `$(...)` not backticks
- **No `cd` in functions:** Use absolute paths via `$SCRIPT_DIR`, `$BUILD_ROOT`, `$HOMELAB_ROOT`

### Naming Conventions
- **Constants/globals:** `UPPERCASE_WITH_UNDERSCORES` (`HOMELAB_ROOT`, `BUILD_ROOT`, `DRY_RUN`)
- **Local variables:** `snake_case` (`host_dir`, `config_file`, `build_dir`)
- **Functions:** `snake_case` (`render_template`, `deploy_run`, `filter_hosts`)
- **Module directories:** `kebab-case` (`pve-gpu-passthrough`, `apt-upgrade`)
- **Arrays for failures:** `DEPLOY_FAILED_HOSTS`, `FAILED_MODULES`

### Imports & Libraries
Every `deploy.sh` **MUST** source `lib/common.sh` first:
```bash
source "$(dirname "$0")/../lib/common.sh"
```
This provides: `set -e`, `HOMELAB_ROOT`, `hosts`, `filter_hosts`, `render_template`,
`prepare_build_dir`, `show_build_diff`, `diff_remote_config`, `diff_remote_build`,
`parse_common_flags` (`--dry-run`, `--force`), `deploy_init`/`deploy_run`/`deploy_finish`, and print helpers.

Remote `install.sh` scripts cannot source `common.sh`. Instead they defensively source
`lib/utils.sh` with inline fallbacks:
```bash
if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() { ... }
    print_sub() { echo "    $*"; }
fi
```

### Output Functions (from `lib/print.sh`)
Use these instead of raw `echo`:
- `print_header "Title"` -- section header (`=== Title ===`)
- `print_action "Step"` -- action (`==> Step`)
- `print_sub "Detail"` -- indented detail
- `print_ok "Done"` -- success (`✓ Done`)
- `print_warn "Issue"` -- warning (`✗ Warning: Issue`)
- `print_error "Msg"` -- error to stderr

### Error Handling
- Check required files/vars early; fail fast with `exit 1`
- Track failed hosts in `DEPLOY_FAILED_HOSTS[@]` rather than aborting
- Use `trap` for cleanup of temporary files outside standard `build/` dirs
- Diff commands must use `|| true` to prevent `set -e` from aborting on differences
- Exit `0` when a module is not applicable to a host (graceful skip, not failure)

### ShellCheck Directives
Use sparingly, with an explanatory comment:
```bash
# shellcheck disable=SC2086  # intentional word splitting, paths are controlled
# shellcheck source=/dev/null  # dynamic source path
# shellcheck disable=SC1090    # alternative for dynamic source
```

### Secrets
- Stored in `.env` files (e.g., `telegram.env`) -- **NEVER** commit these
- All `.env` files are gitignored; only `.env.example` files are tracked
- Scripts must verify secret file existence before running

---

## 3. Architecture & Patterns

### Module Structure
Each top-level directory is a self-contained module:
```
<module>/
  deploy.sh          # Entry point (runs locally)
  templates/         # Config templates with ${VAR} placeholders
  configs/           # Static config files (some have per-host subdirs)
  scripts/install.sh # Runs on remote host after scp
  build/             # Gitignored scratch space for rendered configs
```

### deploy.sh Canonical Pattern
Every `deploy.sh` follows this exact six-phase structure:

```bash
#!/bin/bash
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"

# Phase 1: Parse flags
parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

# Phase 2: Filter hosts
read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature <module>)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping <module> (not applicable to $1)"
    exit 0
fi

# Phase 3: Pre-validation (optional -- check secrets, required files)

# Phase 4: Per-host deploy function
deploy() {
    local host="$1"
    local build_dir="$BUILD_ROOT/$host"

    # Query host config:  hosts get "$host" "feature.key" "default"
    # Prepare build:      prepare_build_dir "$build_dir"
    # Render templates:   render_template "tpl" "out" VAR=VAL
    # Diff remote:        diff_remote_config "$host" local_file remote_path
    # Dry-run gate:       [[ "$DRY_RUN" == "true" ]] && return 0
    # SCP to remote:      scp to /tmp/homelab-<module>/
    # Run installer:      ssh "$host" "cd /tmp/homelab-<module> && ./scripts/install.sh"
}

# Phase 5: Execute
deploy_init "<Module Name>"
deploy_run deploy $HOSTS
deploy_finish
```

### install.sh Pattern (remote)
```bash
#!/bin/bash
set -e
HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
# Source utils defensively (see Imports section above)
# Use file_needs_update/copy_if_changed/backup_and_copy_if_changed for idempotent writes
# Idempotent package install (check command -v first)
# Stop service -> copy configs -> enable+start service
```

### hosts.conf Queries
Never hardcode hostnames. Use the `hosts` command:
```bash
hosts list                          # all hosts
hosts list --feature telegraf       # hosts with a feature
hosts get ace apcupsd.role "slave"  # get config value with default
hosts has ace telegraf               # boolean check
```

### Key Variations Between Modules
- **Template rendering:** Some use `render_template` with `VAR=VAL`, others use plain `cp`
- **Config overlay:** Some modules support per-host config dirs (e.g., `configs/$host/`)
- **Root requirements:** PVE modules hard-require root; docker uses sudo fallback; ssh needs no root
- **Env file passing:** Some modules generate an `env` file for `install.sh` to source
- **GPU cmdline safety:** `pve-gpu-passthrough` must include `root=ZFS=rpool/ROOT/pve-1` in managed cmdline and refuse deploy/install if missing; deploy/install must also validate dataset `rpool/ROOT/pve-1` exists on target host

---

## 4. Git & Workflow

- **Branching:** Feature branches, merge to `main`
- **Commits:** Semantic messages: `feat:`, `fix:`, `refactor:`, `docs:`
- **Pre-commit:** Run `./validate.sh` before committing
- **CI:** GitHub Actions validates on push/PR to `main` (ShellCheck + YAML lint + dry-run)

### Agent Protocol
1. **Understand:** Read the module's `deploy.sh`, `hosts.conf` entries, and `install.sh`
2. **Plan:** Identify changes to templates, configs, or logic
3. **Edit:** Follow existing patterns exactly -- match the six-phase `deploy.sh` structure
4. **Verify:** Run `./validate.sh` and module-specific dry-runs before committing
