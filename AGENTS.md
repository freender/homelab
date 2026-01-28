# Agent Instructions for Homelab Repository

## Project Overview
This repository contains shell-based infrastructure automation for a Proxmox homelab.
It uses modular Bash scripts to deploy configurations to hosts defined in `hosts.conf`.
**Primary Language:** Bash (Shell)
**Config Format:** YAML (`hosts.conf`)
**Hosts:** Proxmox cluster (ace, bray, clovis), PBS (xur), and VMs.

---

## 1. Build, Lint, and Test Commands

### Validation (Run before committing)
Run the full validation suite which includes linting (ShellCheck), YAML validation, and dry-run deployments for all modules.
```bash
./validate.sh
```

### Linting
Lint all shell scripts using ShellCheck.
```bash
find . -name '*.sh' -not -path './.bin/*' -exec shellcheck -S warning {} +
```

### Running "Tests" (Dry Runs)
Since this is an infrastructure repo, "testing" primarily means performing a dry-run deployment to verify configuration generation and script logic without applying changes.

**Run a single test (Dry run for one module on one host):**
```bash
# Syntax: cd <module> && ./deploy.sh --dry-run <host>
cd apcupsd && ./deploy.sh --dry-run ace
```

**Run dry-run for all hosts in a module:**
```bash
cd apcupsd && ./deploy.sh --dry-run all
```

### Operational Debugging
To trace execution during a deploy:
```bash
bash -x apcupsd/deploy.sh ace
```

**Post-Deploy Verification:**
```bash
ssh <host> "systemctl is-active --quiet <service>"
ssh <host> "systemctl status <service>"
ssh <host> "journalctl -u <service> -n 50"
```

---

## 2. Code Style & Conventions

### Bash formatting and Standards
- **Shebang:** Always start with `#!/bin/bash`.
- **Indentation:** Use **4 spaces** for indentation. No tabs.
- **Strict Mode:** All scripts must handle errors. `lib/common.sh` sets `set -e`.
- **Conditionals:** Use `[[ ... ]]` instead of `[ ... ]`.
- **Quoting:** **ALWAYS** quote variables: `"$VAR"`, `"$host"`, `"${ARRAY[@]}"`.
- **Command Substitution:** Use `$(...)` instead of backticks.

### Naming Conventions
- **Constants/Globals:** `UPPERCASE_WITH_UNDERSCORES` (e.g., `HOMELAB_ROOT`).
- **Variables/Locals:** `snake_case` or `lowercase` (e.g., `host_dir`, `config_file`).
- **Functions:** `snake_case` (e.g., `render_template`, `deploy_run`).
- **Module Names:** `kebab-case` (matching directory names).

### Error Handling
- **Dependencies:** Check for required files/vars early.
- **Exit Codes:** Return `1` on failure, `0` on success.
- **Failures:** `deploy.sh` should track failed hosts in `${DEPLOY_FAILED_HOSTS[@]}` rather than exiting immediately if possible, but `set -e` will catch unhandled errors.
- **Cleanup:** Use `trap` if creating temporary files outside standard build dirs.

### Imports & Libraries
- **Common Lib:** Every `deploy.sh` **MUST** source `lib/common.sh`.
  ```bash
  source "$(dirname "$0")/../lib/common.sh"
  ```
- **Utils:** Use functions from `lib/utils.sh` (sourced by common) for remote-safe operations.
- **Output:** Use `print_action`, `print_ok`, `print_warn`, `print_sub` from `lib/print.sh`.

---

## 3. Architecture & Patterns

### Module Structure
Each directory (e.g., `apcupsd`, `telegraf`) is a self-contained module:
- `deploy.sh`: Main entry point.
- `hosts.conf` (root): Controls which hosts get which module.
- `templates/`: Config templates with `${VAR}` placeholders.
- `configs/`: Static configuration files.
- `scripts/`: Helper scripts to run on the remote host (installers).
- `build/`: Local scratch space (gitignored) for rendering configs before scp.

### Deployment Pattern
1.  **Parse Flags:** `parse_common_flags "$@"` handles `--dry-run`.
2.  **Filter Hosts:** `filter_hosts` ensures the module only runs on relevant hosts.
3.  **Render:** Use `render_template "tpl" "out" VAR=VAL`.
4.  **Diff:** Use `prepare_build_dir` and `show_build_diff` to show changes.
5.  **Staging:** `scp` build directory to `/tmp/homelab-<module>/` on remote.
6.  **Execution:** `ssh` to remote and run a script (e.g., `install.sh`) inside the staging dir.
7.  **Idempotency:** Remote scripts should verify state before restarting services.

### Configuration (`hosts.conf`)
- Central source of truth.
- YAML format processed by `yq`.
- **Do not** hardcode hostnames in scripts; query them:
  ```bash
  hosts list --feature myfeature
  hosts get myhost mykey "default_value"
  ```

### Secrets
- **Storage:** `.env` files or specific secret files (e.g., `telegram.env`).
- **Git:** NEVER commit secrets. Ensure secret files are in `.gitignore`.
- **Check:** Scripts should verify secret file existence before running.

---

## 4. Git & Workflow

- **Branching:** Work on feature branches, merge to `main`.
- **Commits:** Use semantic commit messages:
  - `feat: ...` for new modules/capabilities.
  - `fix: ...` for bug fixes.
  - `refactor: ...` for code cleanup.
- **Safety:** Verify `validate.sh` passes before asking for a review or commit.
- **Agent Protocol:**
    1.  **Understand:** Read `deploy.sh` and `hosts.conf` to grasp scope.
    2.  **Plan:** Identify necessary changes to templates or logic.
    3.  **Edit:** Apply changes using `edit` or `write` tools.
    4.  **Verify:** Run `./validate.sh` and specific dry-runs to ensure no regressions.
