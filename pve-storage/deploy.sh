#!/bin/bash
# Deploy PVE storage definitions
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
HOSTS_FILE="$HOMELAB_ROOT/hosts.conf"

normalize_storage_name() {
    local name="$1"
    local normalized
    normalized=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')
    normalized="${normalized##_}"
    normalized="${normalized%%_}"
    echo "$normalized"
}

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-storage)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-storage (not applicable to $1)"
    exit 0
fi

deploy() {
    local host="$1"
    local host_type
    local storage_count
    local build_dir="$BUILD_ROOT/$host"
    local plan_file="$build_dir/storage-plan.conf"
    local index

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    if [[ "$host_type" != "pve" ]]; then
        print_warn "Unsupported host type for $host: $host_type"
        return 1
    fi

    storage_count=$(yq e ".\"$host\".features.pve-storage.storages | length" "$HOSTS_FILE")
    if [[ "$storage_count" == "0" || "$storage_count" == "null" ]]; then
        print_warn "No pve-storage entries defined for $host"
        return 1
    fi

    prepare_build_dir "$build_dir"
    {
        printf 'STORAGE_COUNT=%q\n' "$storage_count"
    } > "$plan_file"

    for (( index=0; index<storage_count; index++ )); do
        local name server datastore username fingerprint password_var_name normalized_name

        name=$(yq e ".\"$host\".features.pve-storage.storages[$index].name" "$HOSTS_FILE")
        server=$(yq e ".\"$host\".features.pve-storage.storages[$index].server" "$HOSTS_FILE")
        datastore=$(yq e ".\"$host\".features.pve-storage.storages[$index].datastore" "$HOSTS_FILE")
        username=$(yq e ".\"$host\".features.pve-storage.storages[$index].username" "$HOSTS_FILE")
        fingerprint=$(yq e ".\"$host\".features.pve-storage.storages[$index].fingerprint" "$HOSTS_FILE")

        if [[ -z "$name" || "$name" == "null" || -z "$server" || "$server" == "null" || -z "$datastore" || "$datastore" == "null" || -z "$username" || "$username" == "null" || -z "$fingerprint" || "$fingerprint" == "null" ]]; then
            print_warn "Invalid pve-storage entry at index $index for $host"
            return 1
        fi

        normalized_name=$(normalize_storage_name "$name")
        password_var_name="PBS_${normalized_name}_PASSWORD"

        {
            printf 'STORAGE_%d_NAME=%q\n' "$index" "$name"
            printf 'STORAGE_%d_SERVER=%q\n' "$index" "$server"
            printf 'STORAGE_%d_DATASTORE=%q\n' "$index" "$datastore"
            printf 'STORAGE_%d_USERNAME=%q\n' "$index" "$username"
            printf 'STORAGE_%d_FINGERPRINT=%q\n' "$index" "$fingerprint"
            printf 'STORAGE_%d_PASSWORD_VAR=%q\n' "$index" "$password_var_name"
        } >> "$plan_file"
    done

    show_build_diff "$build_dir"

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-storage/"
        print_sub "Configured storages:"
        for (( index=0; index<storage_count; index++ )); do
            local entry_name
            entry_name=$(yq e ".\"$host\".features.pve-storage.storages[$index].name" "$HOSTS_FILE")
            print_sub "  - $entry_name"
        done
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-storage && mkdir -p /tmp/homelab-pve-storage/build /tmp/homelab-pve-storage/lib"
    scp -q "$plan_file" "$host:/tmp/homelab-pve-storage/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-storage/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-storage/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-storage && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE deploy requires root SSH user' >&2; exit 1; fi && ./scripts/install.sh"
}

deploy_init "PVE Storage"
deploy_run deploy $HOSTS
deploy_finish
