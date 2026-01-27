#!/bin/bash
# lib/common.sh - Shared deployment functions
# Source this from module deploy.sh scripts:
#   source "$(dirname "$0")/../lib/common.sh"
#
# Dependencies: yq (auto-installed if missing)

set -e

source "$(dirname "${BASH_SOURCE[0]}")/utils.sh"

# Resolve HOMELAB_ROOT (parent of lib/)
HOMELAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# -----------------------------------------------------------------------------
# yq Installation
# -----------------------------------------------------------------------------

YQ_VERSION="v4.44.1"

ensure_yq() {
    command -v yq &>/dev/null && return 0
    
    local yq_bin="${HOMELAB_ROOT}/.bin/yq"
    local arch
    arch=$(uname -m)
    case "$arch" in
        x86_64)  arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *)       echo "Error: Unsupported architecture: $arch" >&2; return 1 ;;
    esac
    
    local url="https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${arch}"
    
    echo "==> Installing yq ${YQ_VERSION}..." >&2
    mkdir -p "$(dirname "$yq_bin")"
    
    if command -v curl &>/dev/null; then
        curl -fsSL "$url" -o "$yq_bin" || { echo "Error: Failed to download yq" >&2; return 1; }
    elif command -v wget &>/dev/null; then
        wget -q "$url" -O "$yq_bin" || { echo "Error: Failed to download yq" >&2; return 1; }
    else
        echo "Error: curl or wget required to install yq" >&2
        return 1
    fi
    
    chmod +x "$yq_bin"
    export PATH="${HOMELAB_ROOT}/.bin:$PATH"
    echo "==> yq installed to $yq_bin" >&2
}

# Auto-install yq on source
ensure_yq || exit 1

# -----------------------------------------------------------------------------
# Host Registry
# -----------------------------------------------------------------------------

# Global hosts file path (optional override by modules)
HOSTS_FILE=""

resolve_hosts_file() {
    if [[ -n "$HOSTS_FILE" ]]; then
        echo "$HOSTS_FILE"
        return 0
    fi

    echo "$HOMELAB_ROOT/hosts.conf"
}

# Unified hosts command
# Usage:
#   hosts list                      # all hosts
#   hosts list --type pve           # hosts by type
#   hosts list --feature telegraf   # hosts by feature
#   hosts get <host> <key> [default] # get host property
#   hosts has <host> <feature>      # check if host has feature (boolean)
hosts() {
    local cmd="$1"
    shift
    
    local hosts_file
    hosts_file=$(resolve_hosts_file)

    case "$cmd" in
        list)
            [[ ! -f "$hosts_file" ]] && { echo "Error: hosts file not found: $hosts_file" >&2; return 1; }
            
            local type="" feature=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --type)
                        type="$2"
                        shift 2
                        ;;
                    --feature)
                        feature="$2"
                        shift 2
                        ;;
                    *)
                        echo "Error: Unknown option: $1" >&2
                        return 1
                        ;;
                esac
            done
            
            if [[ -n "$type" ]]; then
                yq e "to_entries | .[] | select(.value.type == \"$type\") | .key" "$hosts_file" | tr '\n' ' '
            elif [[ -n "$feature" ]]; then
                yq e "to_entries | .[] | select(.value | has(\"$feature\")) | .key" "$hosts_file" | tr '\n' ' '
            else
                yq e 'keys | .[]' "$hosts_file" | tr '\n' ' '
            fi
            ;;
            
        get)
            [[ ! -f "$hosts_file" ]] && { echo "Error: hosts file not found: $hosts_file" >&2; return 1; }
            
            local host="$1"
            local key="$2"
            local default="${3:-}"
            
            [[ -z "$host" ]] && { echo "Error: host required" >&2; return 1; }
            [[ -z "$key" ]] && { echo "Error: key required" >&2; return 1; }
            
            local value
            value=$(yq e ".\"$host\".${key} // \"\"" "$hosts_file")
            
            if [[ -n "$value" && "$value" != "null" ]]; then
                echo "$value"
            elif [[ -n "$default" ]]; then
                echo "$default"
            else
                return 1
            fi
            ;;
            
        has)
            [[ ! -f "$hosts_file" ]] && { echo "Error: hosts file not found: $hosts_file" >&2; return 1; }
            
            local host="$1"
            local feature="$2"
            
            [[ -z "$host" ]] && { echo "Error: host required" >&2; return 1; }
            [[ -z "$feature" ]] && { echo "Error: feature required" >&2; return 1; }
            
            yq e ".\"$host\" | has(\"$feature\")" "$hosts_file" | grep -q "true"
            ;;
            
        *)
            echo "Usage: hosts {list|get|has} ..." >&2
            return 1
            ;;
    esac
}

# -----------------------------------------------------------------------------
# Host Filtering (for deploy.sh argument handling)
# -----------------------------------------------------------------------------

# Filter hosts based on command line argument and supported list
# Usage: 
#   SUPPORTED_HOSTS=(ace bray clovis xur)
#   if ! HOSTS=$(filter_hosts "$1" "${SUPPORTED_HOSTS[@]}"); then
#       echo "==> Skipping module (not applicable to $1)"
#       exit 0
#   fi
filter_hosts() {
    local requested="${1:-all}"
    shift
    local supported=("$@")
    
    # "all" or empty = return all supported hosts
    if [[ "$requested" == "all" || -z "$requested" ]]; then
        echo "${supported[*]}"
        return 0
    fi
    
    # Check if requested host is in supported list
    for host in "${supported[@]}"; do
        if [[ "$host" == "$requested" ]]; then
            echo "$requested"
            return 0
        fi
    done
    
    # Host not supported by this module
    return 1
}

# -----------------------------------------------------------------------------
# Template Rendering
# -----------------------------------------------------------------------------

# Render template with variable substitution
# Usage: render_template TEMPLATE OUTPUT VAR1=val1 VAR2=val2 ...
# Replaces ${VAR1}, ${VAR2}, etc. in template with provided values
# Validates that all placeholders are replaced (fails if unreplaced vars found)
render_template() {
    local template="$1" output="$2"
    shift 2
    
    [[ ! -f "$template" ]] && { echo "Error: Template not found: $template" >&2; return 1; }
    
    local content
    content=$(cat "$template")
    
    # Process each VAR=value argument
    for arg in "$@"; do
        local var="${arg%%=*}"
        local val="${arg#*=}"
        content="${content//\$\{$var\}/$val}"
    done
    
    printf '%s\n' "$content" > "$output"
    
    # Validate: check for unreplaced placeholders
    local unreplaced
    unreplaced=$(grep -oE '\$\{[A-Z_]+\}' "$output" 2>/dev/null || true)
    if [[ -n "$unreplaced" ]]; then
        print_warn "Unreplaced placeholders in $(basename "$output"):"
        echo "$unreplaced" | sort -u | sed 's/^/    /' >&2
        return 1
    fi
}

# -----------------------------------------------------------------------------
# Deployment Helper Functions
# -----------------------------------------------------------------------------

# Global flags
DRY_RUN=${DRY_RUN:-false}


# Parse common deployment flags
# Usage: parse_common_flags "$@"
# Sets DRY_RUN global and modifies positional parameters
# Call with: parse_common_flags "$@"; set -- "${PARSED_ARGS[@]}"
parse_common_flags() {
    PARSED_ARGS=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run|-n)
                DRY_RUN=true
                shift
                ;;
            *)
                PARSED_ARGS+=("$1")
                shift
                ;;
        esac
    done
}

# Prepare build directory with diff support
# Usage: prepare_build_dir BUILD_DIR
# Moves existing build to .prev for diffing
prepare_build_dir() {
    local build_dir="$1"
    
    # Preserve previous build for diffing
    if [[ -d "$build_dir" ]]; then
        rm -rf "${build_dir}.prev"
        mv "$build_dir" "${build_dir}.prev"
    fi
    mkdir -p "$build_dir"
}

# Show diff between current and previous build
# Usage: show_build_diff BUILD_DIR
show_build_diff() {
    local build_dir="$1"
    local prev_dir="${build_dir}.prev"
    
    if [[ ! -d "$prev_dir" ]]; then
        print_sub "No previous build to compare"
        return 0
    fi
    
    local diff_output
    diff_output=$(diff -rq "$prev_dir" "$build_dir" 2>/dev/null || true)
    
    if [[ -z "$diff_output" ]]; then
        print_sub "No changes from previous build"
    else
        print_sub "Changes from previous build:"
        diff -ru "$prev_dir" "$build_dir" 2>/dev/null | head -100 || true
    fi
}

# Deploy a file via scp with ownership and permissions
# Usage: deploy_file local_file host remote_path [mode] [owner]
deploy_file() {
    local src="$1"
    local host="$2"
    local dest="$3"
    local mode="${4:-644}"
    local owner="${5:-root:root}"
    
    scp -q "$src" "${host}:/tmp/homelab-deploy-tmp"
    ssh "$host" "mv /tmp/homelab-deploy-tmp '$dest' && chown $owner '$dest' && chmod $mode '$dest'"
}

# Deploy a file and make it executable
# Usage: deploy_script local_file host remote_path
deploy_script() {
    local src="$1"
    local host="$2"
    local dest="$3"
    
    deploy_file "$src" "$host" "$dest" "755" "root:root"
}

# Ensure remote directory exists
# Usage: ensure_remote_dir host /path/to/dir [mode]
ensure_remote_dir() {
    local host="$1"
    local dir="$2"
    local mode="${3:-755}"
    
    ssh "$host" "mkdir -p '$dir' && chmod $mode '$dir'"
}

# -----------------------------------------------------------------------------
# Deployment Framework
# -----------------------------------------------------------------------------

# Global state
DEPLOY_MODULE=""
declare -ga DEPLOY_FAILED_HOSTS=()

# Initialize deployment
# Usage: deploy_init "Module Name"
deploy_init() {
    DEPLOY_MODULE="$1"
    DEPLOY_FAILED_HOSTS=()
}

# Run deployment across hosts
# Usage: deploy_run <deploy_function> <hosts...>
# The deploy function receives host as $1, should return 0 on success
deploy_run() {
    local deploy_fn="$1"
    shift
    local hosts_list="$*"
    
    print_action "Deploying $DEPLOY_MODULE"
    print_sub "Hosts: $hosts_list"
    echo ""
    
    for host in $hosts_list; do
        print_action "Deploying to $host..."
        
        if "$deploy_fn" "$host"; then
            print_ok "Deployed to $host"
        else
            print_warn "Failed to deploy to $host"
            DEPLOY_FAILED_HOSTS+=("$host")
        fi
        echo ""
    done
}

# Finish deployment and report results
# Usage: deploy_finish
# Returns: 0 if all succeeded, 1 if any failed
deploy_finish() {
    print_action "Deployment complete!"
    
    if [[ ${#DEPLOY_FAILED_HOSTS[@]} -gt 0 ]]; then
        echo ""
        print_warn "Failed hosts: ${DEPLOY_FAILED_HOSTS[*]}"
        return 1
    fi
    return 0
}

# -----------------------------------------------------------------------------
# Service Helpers
# -----------------------------------------------------------------------------

# Enable and start a systemd service
# Usage: enable_remote_service host service
enable_remote_service() {
    local host="$1"
    local service="$2"
    ssh "$host" "systemctl enable --now $service"
}

# Verify a systemd service is running
# Usage: verify_remote_service host service
# Returns: 0 if active, 1 if not
verify_remote_service() {
    local host="$1"
    local service="$2"
    
    if ssh "$host" "systemctl is-active --quiet $service" 2>/dev/null; then
        print_ok "$service running"
        return 0
    else
        print_warn "$service not running"
        ssh "$host" "systemctl status $service --no-pager -l" 2>/dev/null || true
        return 1
    fi
}
