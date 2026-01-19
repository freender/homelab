# Agent Instructions for Homelab Repository

**Repository:** `git@github.com:freender/homelab.git`  
**Type:** Shell-based infrastructure automation for Proxmox homelab cluster  
**Primary Language:** Bash/Shell scripts  
**Infrastructure:** 3-node Proxmox Ceph cluster (ace, bray, clovis), PBS backup server (xur), VMs, remote NAS

---

## Commands Reference

### Deployment Commands

```bash
# Deploy all modules to all hosts
./deploy-all.sh

# Deploy all modules to a specific host
./deploy-all.sh <hostname>

# Deploy single module to all its supported hosts
cd <module>/ && ./deploy.sh all

# Deploy single module to specific hosts
cd <module>/ && ./deploy.sh <host1> <host2>
```

### No Build/Test/Lint Commands

This repository has **no formal build system, test framework, or linters**. Verification is manual:

```bash
# Verify deployment by checking service status
ssh <host> "systemctl status <service>"

# Check logs
ssh <host> "journalctl -u <service> -n 50"

# Manual functional tests exist in some modules
# Example: apcupsd/scripts/test-shutdown.sh
```

### Module Structure

```
homelab/
├── deploy-all.sh              # Master orchestrator
├── <module>/
│   ├── deploy.sh              # Module deployment script
│   ├── configs/               # Per-host configurations
│   ├── scripts/               # Installation/utility scripts
│   └── README.md              # Module documentation
```

**Modules:** apcupsd, telegraf, zfs, docker, ssh, pve-interfaces, pve-gpu-passthrough, filebot

---

## Code Style Guidelines

### Shell Script Standards

**Error Handling:**
```bash
#!/bin/bash
set -e    # Exit on error (REQUIRED for all scripts)
set -u    # Exit on undefined variables (OPTIONAL but recommended)
```

**Script Header:**
```bash
#!/bin/bash
# Brief description of what this script does
# Usage: ./script.sh [arguments]
```

**Directory Resolution:**
```bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# OR for sourced scripts:
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
```

**Host Filtering Pattern (for deploy.sh):**
```bash
# Supported hosts for this module
SUPPORTED_HOSTS=("ace" "bray" "clovis" "xur")

# Skip if host not applicable
if [[ -n "${1:-}" && "$1" != "all" ]]; then
    if [[ ! " ${SUPPORTED_HOSTS[*]} " =~ " $1 " ]]; then
        echo "==> Skipping <module> (not applicable to $1)"
        exit 0  # Exit 0 = graceful skip
    fi
fi
```

### Naming Conventions

**Variables:**
- `UPPERCASE_WITH_UNDERSCORES` for constants and environment variables
- `lowercase` for local variables in simple scripts
- Use `${VAR}` bracing for clarity, `"${VAR}"` for safety

**Files:**
- `lowercase-with-dashes.sh` for scripts
- `lowercase.conf` for configuration files
- `README.md` for documentation (capitalized)

**Hosts:**
- Proxmox nodes: `ace`, `bray`, `clovis`
- Backup server: `xur`
- VMs: `helm`, `tower`
- Remote NAS: `cottonwood`, `cinci`

### Deployment Pattern

**Standard deploy.sh structure:**
```bash
#!/bin/bash
set -e

SUPPORTED_HOSTS=("host1" "host2")

# Host filtering (see above)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Validate prerequisites (secrets, configs)
if [[ ! -f "$SCRIPT_DIR/required-file" ]]; then
    echo "ERROR: required-file not found!"
    exit 1
fi

echo "=== Deploying <module> ==="

for HOST in $HOSTS; do
    echo "=== Deploying to $HOST ==="
    
    # Create temp directory on target
    ssh "$HOST" "rm -rf /tmp/homelab-<module> && mkdir -p /tmp/homelab-<module>"
    
    # Copy files
    scp -r "$SCRIPT_DIR"/* "$HOST:/tmp/homelab-<module>/"
    
    # Run installer
    ssh "$HOST" "cd /tmp/homelab-<module> && chmod +x scripts/install.sh && ./scripts/install.sh"
    
    echo ""
done

echo "=== Deployment complete ==="
```

### Configuration Management

**Secrets:**
- Store in `.env` or `telegram.env` files (gitignored)
- Provide `.env.example` templates
- Validate existence before deployment
- **NEVER commit secrets to git**

**Per-host configs:**
- Organize in `<module>/<hostname>/` subdirectories
- Use hostname as directory name
- Example: `pve-interfaces/ace/interfaces`, `apcupsd/configs/ace/apcupsd.conf`

**Heredocs for config files:**
```bash
# Use heredocs instead of manual editing
cat > /path/to/config << 'EOF'
config content here
variables not expanded with single quotes
EOF

# Use unquoted delimiter for variable expansion
cat > /path/to/config << EOF
config with ${VARIABLE} expansion
EOF
```

### SSH Operations

**Remote commands:**
```bash
# Single command
ssh "$HOST" "command"

# Multiple commands (use shell quoting)
ssh "$HOST" "cmd1 && cmd2 && cmd3"

# Quiet operation for service checks
ssh "$HOST" "systemctl is-active --quiet service"

# Handle errors gracefully
ssh "$HOST" "command || true"
```

**File transfers:**
```bash
# Copy single file
scp "$LOCAL_FILE" "${HOST}:/remote/path/"

# Copy directory recursively
scp -r "$LOCAL_DIR" "${HOST}:/remote/path/"

# Set permissions after copy
ssh "$HOST" "chown root:root /path/file && chmod 644 /path/file"
```

### Output and Logging

**Consistent output format:**
```bash
echo "=== Section Header ==="
echo "==> Action starting..."
echo "    Substep details..."
echo "    ✓ Success indicator"
echo "    ✗ Warning: Problem description"
echo ""  # Blank line between sections
```

**Exit codes:**
- `exit 0` = success or graceful skip
- `exit 1` = failure (triggers error in deploy-all.sh)
- Use `set -e` to auto-exit on command failures

### Types and Validation

**Bash has no type system.** Use validation instead:

```bash
# Check file exists
if [[ ! -f "$FILE" ]]; then
    echo "ERROR: File not found: $FILE"
    exit 1
fi

# Check directory exists
if [[ ! -d "$DIR" ]]; then
    mkdir -p "$DIR"
fi

# Check command exists
if ! command -v telegraf &> /dev/null; then
    echo "Installing telegraf..."
    apt-get install -y telegraf
fi

# Validate arguments
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <required-arg>"
    exit 1
fi
```

### Python Scripts (Minimal Usage)

**Style (when needed):**
- Python 3
- Shebang: `#!/usr/bin/env python3`
- Used only for complex data processing (e.g., telegraf custom collectors)
- Keep simple, no external dependencies if possible

---

## Documentation Style

**Module README.md structure:**
```markdown
# Module Name

Brief description (1-2 sentences)

## Architecture

Hardware/software details

## Installation

**SCOPE:** PER-NODE or CLUSTER-WIDE
**NODES:** hostname(s)
**REBUILD:** Required or Skip

1) Numbered steps with commands:
\`\`\`bash
command without ssh wrapper
\`\`\`

2) Continue...

## Quick Reference

**Key:** Value
**Command:** `example`
```

**Philosophy:** Command-focused, minimal prose, numbered steps. Commands are copy-paste ready and assume you're ON the target machine (no `ssh host` wrapper in docs unless reaching a different remote).

---

## Git Workflow

**Commits:**
- Conventional style: `add`, `update`, `fix`, `refactor`, `docs`
- Concise messages focused on "why" not "what"
- Example: `update: add memory metrics to telegraf configs`

**Branches:**
- Work directly on `main` for this repository (small team, infrastructure-as-code)
- No formal branching strategy or CI/CD

**Secrets:**
- `.gitignore` blocks: `**/.env`, `**/telegram.env`, `**/*.env`
- Always check before committing: `git status`
- Provide `.env.example` templates for all secret files

---

## Common Tasks

### Adding a New Module

1. Create directory: `mkdir <module>`
2. Create `deploy.sh` with host filtering and standard structure
3. Create `scripts/install.sh` for on-host installation
4. Create `README.md` following documentation style
5. Add module summary to main `README.md`
6. Test: `./deploy-all.sh <test-host>`

### Modifying Configuration

1. Update config file in `<module>/configs/<host>/`
2. Run deployment: `cd <module> && ./deploy.sh <host>`
3. Verify: `ssh <host> "cat /path/to/deployed/config"`
4. Check service: `ssh <host> "systemctl status <service>"`

### Adding Secrets

1. Create `.env.example` template with placeholder values
2. Document in module README under "Installation"
3. Add to `.gitignore` pattern (already covered by `**/*.env`)
4. Add validation in `deploy.sh` before deployment

### Debugging Deployment

```bash
# Check deploy-all.sh found modules
./deploy-all.sh 2>&1 | grep "Deploying"

# Run single module with set -x for debugging
bash -x <module>/deploy.sh <host>

# Check SSH connectivity
ssh <host> "echo 'Connected to $(hostname)'"

# View service logs
ssh <host> "journalctl -u <service> -f"
```

---

## Infrastructure Context

**DNS:**
- Home: `*.freender.internal`
- Remote: `cottonwood.internal`, `cinci.internal`

**Services:**
- VictoriaMetrics: `victoria-metrics.freender.internal:8428`
- Traefik: VIP-based HA

**Hardware:**
- ace: Intel UHD 630 GPU passthrough
- bray: Intel Alder Lake-P iGPU passthrough, UPS-connected
- clovis: NVIDIA RTX 3080 GPU passthrough
- xur: Proxmox Backup Server, UPS-connected

---

## Key Principles

1. **Simplicity over tooling** - Direct bash scripts, no Ansible/Terraform
2. **Explicit error handling** - Always use `set -e`, validate inputs
3. **Graceful host filtering** - Modules skip non-applicable hosts with `exit 0`
4. **SSH-based deployment** - Copy to /tmp, execute, clean up
5. **Secret safety** - Never commit .env files, always validate before deploy
6. **Command-focused docs** - Show exact commands, minimal explanation
7. **Manual verification** - No automated tests, rely on service status checks

---

**Last updated:** 2026-01-18
