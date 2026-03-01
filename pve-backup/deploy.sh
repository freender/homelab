#!/bin/bash
# Deploy Proxmox backup job definitions
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
HOSTS_FILE="$HOMELAB_ROOT/hosts.conf"

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-backup)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-backup (not applicable to $1)"
    exit 0
fi

deploy() {
    local host="$1"
    local host_type
    local job_count
    local build_dir="$BUILD_ROOT/$host"
    local plan_file="$build_dir/jobs-plan.conf"
    local index

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    if [[ "$host_type" != "pve" ]]; then
        print_warn "Unsupported host type for $host: $host_type"
        return 1
    fi

    job_count=$(yq e ".\"$host\".features.pve-backup.jobs | length" "$HOSTS_FILE")
    if [[ "$job_count" == "0" || "$job_count" == "null" ]]; then
        print_warn "No pve-backup jobs defined for $host"
        return 1
    fi

    prepare_build_dir "$build_dir"
    {
        printf 'JOB_COUNT=%q\n' "$job_count"
    } > "$plan_file"

    for (( index=0; index<job_count; index++ )); do
        local schedule storage vmid compress mode notes_template notification_mode prune_backups enabled fleecing

        schedule=$(yq e ".\"$host\".features.pve-backup.jobs[$index].schedule" "$HOSTS_FILE")
        storage=$(yq e ".\"$host\".features.pve-backup.jobs[$index].storage" "$HOSTS_FILE")
        vmid=$(yq e ".\"$host\".features.pve-backup.jobs[$index].vmid // \"\"" "$HOSTS_FILE")
        compress=$(yq e ".\"$host\".features.pve-backup.jobs[$index].compress // \"zstd\"" "$HOSTS_FILE")
        mode=$(yq e ".\"$host\".features.pve-backup.jobs[$index].mode // \"snapshot\"" "$HOSTS_FILE")
        notes_template=$(yq e ".\"$host\".features.pve-backup.jobs[$index].notes_template // \"{{guestname}}\"" "$HOSTS_FILE")
        notification_mode=$(yq e ".\"$host\".features.pve-backup.jobs[$index].notification_mode // \"notification-system\"" "$HOSTS_FILE")
        prune_backups=$(yq e ".\"$host\".features.pve-backup.jobs[$index].prune_backups // \"keep-all=1\"" "$HOSTS_FILE")
        enabled=$(yq e ".\"$host\".features.pve-backup.jobs[$index].enabled // \"1\"" "$HOSTS_FILE")
        fleecing=$(yq e ".\"$host\".features.pve-backup.jobs[$index].fleecing // \"0\"" "$HOSTS_FILE")

        if [[ -z "$schedule" || "$schedule" == "null" || -z "$storage" || "$storage" == "null" ]]; then
            print_warn "Invalid pve-backup job at index $index for $host"
            return 1
        fi

        {
            printf 'JOB_%d_SCHEDULE=%q\n' "$index" "$schedule"
            printf 'JOB_%d_STORAGE=%q\n' "$index" "$storage"
            printf 'JOB_%d_VMID=%q\n' "$index" "$vmid"
            printf 'JOB_%d_COMPRESS=%q\n' "$index" "$compress"
            printf 'JOB_%d_MODE=%q\n' "$index" "$mode"
            printf 'JOB_%d_NOTES_TEMPLATE=%q\n' "$index" "$notes_template"
            printf 'JOB_%d_NOTIFICATION_MODE=%q\n' "$index" "$notification_mode"
            printf 'JOB_%d_PRUNE_BACKUPS=%q\n' "$index" "$prune_backups"
            printf 'JOB_%d_ENABLED=%q\n' "$index" "$enabled"
            printf 'JOB_%d_FLEECING=%q\n' "$index" "$fleecing"
        } >> "$plan_file"
    done

    show_build_diff "$build_dir"

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-backup/"
        print_sub "Configured backup jobs:"
        for (( index=0; index<job_count; index++ )); do
            local entry_storage entry_vmid
            entry_storage=$(yq e ".\"$host\".features.pve-backup.jobs[$index].storage" "$HOSTS_FILE")
            entry_vmid=$(yq e ".\"$host\".features.pve-backup.jobs[$index].vmid // \"all\"" "$HOSTS_FILE")
            print_sub "  - storage=$entry_storage vmid=$entry_vmid"
        done
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-backup && mkdir -p /tmp/homelab-pve-backup/build /tmp/homelab-pve-backup/lib"
    scp -q "$plan_file" "$host:/tmp/homelab-pve-backup/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-backup/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-backup/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-backup && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE deploy requires root SSH user' >&2; exit 1; fi && ./scripts/install.sh"
}

deploy_init "PVE Backup Jobs"
deploy_run deploy $HOSTS
deploy_finish
