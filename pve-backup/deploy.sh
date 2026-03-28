#!/bin/bash
source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
CONFIGS_DIR="$SCRIPT_DIR/configs"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
HOSTS_FILE="$HOMELAB_ROOT/hosts.conf"
SECRETS_DIR="$HOMELAB_ROOT/secrets"
PBS_ENV_BACKUP_MAIN="$SECRETS_DIR/pbs-backup-main.env"
PBS_ENV_BACKUP_CINCI="$SECRETS_DIR/pbs-backup-cinci.env"

normalize_storage_name() {
    local name="$1"
    local normalized
    normalized=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')
    normalized="${normalized##_}"
    normalized="${normalized%%_}"
    echo "$normalized"
}

build_standalone_backup_plans() {
    local host="$1"
    local build_dir="$2"
    local storage_plan_file="$build_dir/storage-plan.conf"
    local jobs_plan_file="$build_dir/jobs-plan.conf"
    local storage_count
    local job_count
    local index
    local name server datastore username fingerprint password_var_name normalized_name
    local schedule storage vmid compress mode notes_template notification_mode prune_backups enabled fleecing

    storage_count=$(yq e ".\"$host\".features.pve-backup.pbs_setup.storages | length" "$HOSTS_FILE")
    job_count=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs | length" "$HOSTS_FILE")

    if [[ "$storage_count" == "null" ]]; then
        storage_count="0"
    fi
    if [[ "$job_count" == "null" ]]; then
        job_count="0"
    fi

    if [[ "$storage_count" == "0" && "$job_count" == "0" ]]; then
        return 0
    fi

    printf 'STORAGE_COUNT=%q\n' "$storage_count" > "$storage_plan_file"

    for (( index=0; index<storage_count; index++ )); do
        name=$(yq e ".\"$host\".features.pve-backup.pbs_setup.storages[$index].name" "$HOSTS_FILE")
        server=$(yq e ".\"$host\".features.pve-backup.pbs_setup.storages[$index].server" "$HOSTS_FILE")
        datastore=$(yq e ".\"$host\".features.pve-backup.pbs_setup.storages[$index].datastore" "$HOSTS_FILE")
        username=$(yq e ".\"$host\".features.pve-backup.pbs_setup.storages[$index].username" "$HOSTS_FILE")
        fingerprint=$(yq e ".\"$host\".features.pve-backup.pbs_setup.storages[$index].fingerprint" "$HOSTS_FILE")

        if [[ -z "$name" || "$name" == "null" || -z "$server" || "$server" == "null" || -z "$datastore" || "$datastore" == "null" || -z "$username" || "$username" == "null" || -z "$fingerprint" || "$fingerprint" == "null" ]]; then
            print_warn "Invalid standalone storage entry at index $index for $host"
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
        } >> "$storage_plan_file"
    done

    printf 'JOB_COUNT=%q\n' "$job_count" > "$jobs_plan_file"

    for (( index=0; index<job_count; index++ )); do
        schedule=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].schedule" "$HOSTS_FILE")
        storage=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].storage" "$HOSTS_FILE")
        vmid=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].vmid // \"\"" "$HOSTS_FILE")
        compress=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].compress // \"zstd\"" "$HOSTS_FILE")
        mode=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].mode // \"snapshot\"" "$HOSTS_FILE")
        notes_template=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].notes_template // \"{{guestname}}\"" "$HOSTS_FILE")
        notification_mode=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].notification_mode // \"notification-system\"" "$HOSTS_FILE")
        prune_backups=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].prune_backups // \"keep-all=1\"" "$HOSTS_FILE")
        enabled=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].enabled // \"1\"" "$HOSTS_FILE")
        fleecing=$(yq e ".\"$host\".features.pve-backup.pbs_setup.jobs[$index].fleecing // \"0\"" "$HOSTS_FILE")

        if [[ -z "$schedule" || "$schedule" == "null" || -z "$storage" || "$storage" == "null" ]]; then
            print_warn "Invalid standalone backup job at index $index for $host"
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
        } >> "$jobs_plan_file"
    done
}

build_cluster_config_backup_bundle() {
    local host="$1"
    local build_dir="$2"
    local schedule repository backup_id archive_name ceph_enabled secret_profile pbs_env_source

    repository=$(yq e ".\"$host\".features.pve-backup.proxmox_backup_client.repository // \"\"" "$HOSTS_FILE")
    if [[ -z "$repository" || "$repository" == "null" ]]; then
        return 0
    fi

    schedule=$(yq e ".\"$host\".features.pve-backup.proxmox_backup_client.schedule // \"00:30\"" "$HOSTS_FILE")
    backup_id=$(yq e ".\"$host\".features.pve-backup.proxmox_backup_client.backup_id // \"pve-config\"" "$HOSTS_FILE")
    archive_name=$(yq e ".\"$host\".features.pve-backup.proxmox_backup_client.archive_name // \"etc-pve\"" "$HOSTS_FILE")
    ceph_enabled="false"
    if hosts has "$host" "ceph"; then
        ceph_enabled="true"
    fi

    secret_profile=$(yq e ".\"$host\".features.pve-backup.proxmox_backup_client.secret_profile // \"\"" "$HOSTS_FILE")
    case "$secret_profile" in
        backup-main)
            pbs_env_source="$PBS_ENV_BACKUP_MAIN"
            ;;
        backup-cinci)
            pbs_env_source="$PBS_ENV_BACKUP_CINCI"
            ;;
        "")
            print_warn "proxmox_backup_client.secret_profile not set for $host; skipping config backup bundle"
            return 0
            ;;
        *)
            print_warn "invalid secret_profile '$secret_profile' for $host; expected: backup-main, backup-cinci"
            return 1
            ;;
    esac

    if [[ ! -f "$pbs_env_source" ]]; then
        print_warn "missing secret file for $host: $pbs_env_source"
        return 1
    fi

    render_template "$CONFIGS_DIR/pve-config-backup.sh.tpl" "$build_dir/pve-config-backup.sh" \
        REPOSITORY="$repository" \
        BACKUP_ID="$backup_id" \
        ARCHIVE_NAME="$archive_name" \
        CEPH_ENABLED="$ceph_enabled"
    chmod 700 "$build_dir/pve-config-backup.sh"

    cp "$TEMPLATES_DIR/pve-config-backup.service" "$build_dir/pve-config-backup.service"
    render_template "$TEMPLATES_DIR/pve-config-backup.timer" "$build_dir/pve-config-backup.timer" \
        SCHEDULE="$schedule"
    cp "$pbs_env_source" "$build_dir/pbs.env"

    {
        printf 'REPOSITORY=%q\n' "$repository"
        printf 'BACKUP_ID=%q\n' "$backup_id"
        printf 'ARCHIVE_NAME=%q\n' "$archive_name"
        printf 'CEPH_ENABLED=%q\n' "$ceph_enabled"
    } > "$build_dir/restore-plan.conf"
}

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-backup)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-backup (not applicable to $1)"
    exit 0
fi

validate() {
    local errors=0
    local host secret_profile pbs_env_source file

    for file in pve-config-backup.sh.tpl pbs-tokens.env.example; do
        if [[ ! -f "$CONFIGS_DIR/$file" ]]; then
            print_error "missing config file: $CONFIGS_DIR/$file"
            errors=$((errors + 1))
        fi
    done

    for file in pve-config-backup.service pve-config-backup.timer; do
        if [[ ! -f "$TEMPLATES_DIR/$file" ]]; then
            print_error "missing template: $TEMPLATES_DIR/$file"
            errors=$((errors + 1))
        fi
    done

    for host in "${SUPPORTED_HOSTS[@]}"; do
        secret_profile=$(yq e ".\"$host\".features.pve-backup.proxmox_backup_client.secret_profile // \"\"" "$HOSTS_FILE")
        case "$secret_profile" in
            backup-main)
                pbs_env_source="$PBS_ENV_BACKUP_MAIN"
                ;;
            backup-cinci)
                pbs_env_source="$PBS_ENV_BACKUP_CINCI"
                ;;
            "")
                pbs_env_source=""
                ;;
            *)
                print_error "$host: invalid secret_profile '$secret_profile' (expected: backup-main, backup-cinci)"
                errors=$((errors + 1))
                continue
                ;;
        esac

        if [[ -n "$pbs_env_source" && ! -f "$pbs_env_source" ]]; then
            print_error "$host: missing secret file: $pbs_env_source"
            errors=$((errors + 1))
        fi
    done

    if [[ $errors -gt 0 ]]; then
        print_error "validation failed with $errors error(s); aborting"
        exit 1
    fi
}

deploy() {
    local host="$1"
    local host_type
    local build_dir="$BUILD_ROOT/$host"

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    if [[ "$host_type" != "pve" ]]; then
        print_warn "Unsupported host type for $host: $host_type"
        return 1
    fi

    prepare_build_dir "$build_dir"

    if ! build_standalone_backup_plans "$host" "$build_dir"; then
        return 1
    fi

    if ! build_cluster_config_backup_bundle "$host" "$build_dir"; then
        return 1
    fi

    print_sub "Comparing with remote configs..."
    if [[ -f "$build_dir/pve-config-backup.sh" ]]; then
        diff_remote_config "$host" "$build_dir/pve-config-backup.sh" "/root/pve-config-backup.sh" || true
        diff_remote_config "$host" "$build_dir/pve-config-backup.service" "/etc/systemd/system/pve-config-backup.service" || true
        diff_remote_config "$host" "$build_dir/pve-config-backup.timer" "/etc/systemd/system/pve-config-backup.timer" || true
        diff_remote_config "$host" "$build_dir/pbs.env" "/etc/homelab/pve-config-backup.env" || true
    fi

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-backup/"
        print_sub "Build files:"
        find "$build_dir" -type f | sed "s|$build_dir/|    |"

        if [[ -f "$build_dir/storage-plan.conf" || -f "$build_dir/jobs-plan.conf" ]]; then
            print_sub "Standalone backup subfeature: enabled"
        else
            print_sub "Standalone backup subfeature: disabled"
        fi

        if [[ -f "$build_dir/pve-config-backup.timer" ]]; then
            print_sub "Cluster config backup subfeature: enabled"
        else
            print_sub "Cluster config backup subfeature: disabled"
        fi
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-backup && mkdir -p /tmp/homelab-pve-backup/build /tmp/homelab-pve-backup/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-backup/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-backup/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-backup/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-backup && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE backup deploy requires root SSH user' >&2; exit 1; fi && FORCE_UPDATE='$FORCE_UPDATE' ./scripts/install.sh '$host'"
}

validate
deploy_init "PVE Backup"
deploy_run deploy $HOSTS
deploy_finish
