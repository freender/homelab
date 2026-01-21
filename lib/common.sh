#!/bin/bash
# lib/common.sh - Shared deployment functions
# Source this from module deploy.sh scripts:
#   source "$(dirname "$0")/../lib/common.sh"

set -e

# Resolve HOMELAB_ROOT (parent of lib/)
HOMELAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Hosts registry (loaded per-module)
declare -gA HOST_TYPES=()
declare -gA HOST_FEATURES=()
declare -gA HOST_KV=()
declare -ga HOST_ORDER=()
HOSTS_LOADED=0

load_hosts_file() {
    local file="$1"
    local current_host=""
    local in_features=0

    if [[ ! -f "$file" ]]; then
        echo "Error: hosts file not found: $file" >&2
        return 1
    fi

    HOST_TYPES=()
    HOST_FEATURES=()
    HOST_KV=()
    HOST_ORDER=()
    HOSTS_LOADED=1

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line//[[:space:]]/}" ]] && continue

        if [[ "$line" =~ ^[A-Za-z0-9._-]+:[[:space:]]*$ ]]; then
            current_host="${line%%:*}"
            HOST_ORDER+=("$current_host")
            HOST_TYPES["$current_host"]=""
            HOST_FEATURES["$current_host"]=""
            in_features=0
            continue
        fi

        if [[ -z "$current_host" ]]; then
            continue
        fi

        if [[ "$line" =~ ^[[:space:]]+features:[[:space:]]*$ ]]; then
            in_features=1
            continue
        fi

        if [[ "$line" =~ ^[[:space:]]+-[[:space:]]+(.+)$ && $in_features -eq 1 ]]; then
            local feature="${BASH_REMATCH[1]}"
            feature="${feature%%[[:space:]]#*}"
            feature="${feature%"${feature##*[![:space:]]}"}"
            feature="${feature#"${feature%%[![:space:]]*}"}"
            if [[ -n "$feature" ]]; then
                HOST_FEATURES["$current_host"]+=" $feature"
                HOST_FEATURES["$current_host"]="${HOST_FEATURES["$current_host"]# }"
            fi
            continue
        fi

        if [[ "$line" =~ ^[[:space:]]+[^:]+:[[:space:]]*.*$ ]]; then
            in_features=0
            local key="${line%%:*}"
            local value="${line#*:}"
            key="${key%"${key##*[![:space:]]}"}"
            key="${key#"${key%%[![:space:]]*}"}"
            value="${value%"${value##*[![:space:]]}"}"
            value="${value#"${value%%[![:space:]]*}"}"
            if [[ "$value" =~ ^".*"$ ]]; then
                value="${value:1:${#value}-2}"
            elif [[ "$value" =~ ^'.*'$ ]]; then
                value="${value:1:${#value}-2}"
            fi

            if [[ "$key" == "type" ]]; then
                HOST_TYPES["$current_host"]="$value"
            else
                HOST_KV["$current_host|$key"]="$value"
            fi
        fi
    done < "$file"
}

require_hosts_loaded() {
    if [[ ${HOSTS_LOADED:-0} -ne 1 ]]; then
        echo "Error: hosts file not loaded. Call load_hosts_file first." >&2
        return 1
    fi
}

# -----------------------------------------------------------------------------
# Host Registry Functions
# -----------------------------------------------------------------------------

# Get all hosts of a specific type (pve, pbs, vm, truenas, unraid)
# Usage: get_hosts_by_type pve
get_hosts_by_type() {
    local type="$1"
    require_hosts_loaded || return 1
    local hosts=()
    local host

    for host in "${HOST_ORDER[@]}"; do
        if [[ "${HOST_TYPES[$host]}" == "$type" ]]; then
            hosts+=("$host")
        fi
    done

    printf '%s ' "${hosts[@]}"
}

# Get all hosts with a specific feature
# Usage: get_hosts_with_feature gpu
get_hosts_with_feature() {
    local feature="$1"
    require_hosts_loaded || return 1
    local hosts=()
    local host

    for host in "${HOST_ORDER[@]}"; do
        if [[ " ${HOST_FEATURES[$host]} " == *" $feature "* ]]; then
            hosts+=("$host")
        fi
    done

    printf '%s ' "${hosts[@]}"
}

# Check if host has a feature
# Usage: if host_has_feature bray ups-master; then ...
host_has_feature() {
    local host="$1" feature="$2"
    require_hosts_loaded || return 1
    [[ " ${HOST_FEATURES[$host]} " == *" $feature "* ]]
}

# Get host type
# Usage: get_host_type bray  # returns "pve"
get_host_type() {
    local host="$1"
    require_hosts_loaded || return 1
    echo "${HOST_TYPES[$host]}"
}

# Get all defined hosts
# Usage: get_all_hosts
get_all_hosts() {
    require_hosts_loaded || return 1
    printf '%s ' "${HOST_ORDER[@]}"
}

# Get host key/value from module hosts.conf
# Usage: get_host_kv ace ups.role
get_host_kv() {
    local host="$1" key="$2"
    require_hosts_loaded || return 1
    local value

    value="${HOST_KV[$host|$key]}"
    if [[ -z "$value" ]]; then
        return 1
    fi

    echo "$value"
}

# Get host key/value with default
# Usage: get_host_kv_default ace net.mgmt_ip 0.0.0.0
get_host_kv_default() {
    local host="$1" key="$2" default="$3"
    local value

    value=$(get_host_kv "$host" "$key" || true)
    if [[ -n "$value" ]]; then
        echo "$value"
    else
        echo "$default"
    fi
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
# Deployment Helper Functions
# -----------------------------------------------------------------------------

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
# Config Helpers
# -----------------------------------------------------------------------------

# Load KEY=VALUE config file into shell variables
# Usage: load_kv_file /path/to/config
load_kv_file() {
    local file="$1"

    if [[ ! -f "$file" ]]; then
        echo "Error: config file not found: $file" >&2
        return 1
    fi

    while IFS='=' read -r key value; do
        [[ -z "${key//[[:space:]]/}" ]] && continue
        [[ "$key" == \#* ]] && continue

        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"

        if [[ -n "$key" ]]; then
            printf -v "$key" '%s' "$value"
        fi
    done < "$file"
}

# -----------------------------------------------------------------------------
# Output Helpers
# -----------------------------------------------------------------------------

# Print section header
print_header() {
    echo "=== $* ==="
}

# Print action
print_action() {
    echo "==> $*"
}

# Print sub-action
print_sub() {
    echo "    $*"
}

# Print success
print_ok() {
    echo "    ✓ $*"
}

# Print warning
print_warn() {
    echo "    ✗ Warning: $*"
}
