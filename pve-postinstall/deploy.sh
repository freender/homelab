#!/bin/bash
# Deploy PVE post-install configs
# Usage: ./deploy.sh [host|all]

source "$(dirname "$0")/../lib/common.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build"
PVE_CONFIG_DIR="$SCRIPT_DIR/configs/pve"
CONFIGS_DIR="$SCRIPT_DIR/configs"
HOSTS_FILE="$HOMELAB_ROOT/hosts.conf"
SECRETS_DIR="$HOMELAB_ROOT/secrets"
PBS_ENV_BACKUP_MAIN="$SECRETS_DIR/pbs-backup-main.env"
PBS_ENV_BACKUP_CINCI="$SECRETS_DIR/pbs-backup-cinci.env"
PBS_ENV_DIR_LEGACY="$CONFIGS_DIR/pbs-env"
PBS_ENV_LEGACY_SOURCE="$CONFIGS_DIR/pbs.env"
TELEGRAM_ENV_SOURCE="$SECRETS_DIR/telegram.env"
TELEGRAM_ENV_FALLBACK="$HOMELAB_ROOT/apcupsd/configs/telegram/telegram.env"
INTERFACES_TEMPLATE="$SCRIPT_DIR/templates/pve-interfaces"

PVE_FILES=(
    proxmox.sources
    ceph.sources
    pve-test.sources
    no-nag-script
    pve-remove-nag.sh
)

remote_path_for_file() {
    local file="$1"
    case "$file" in
        proxmox.sources|ceph.sources|pve-test.sources)
            echo "/etc/apt/sources.list.d/$file"
            ;;
        no-nag-script)
            echo "/etc/apt/apt.conf.d/no-nag-script"
            ;;
        pve-remove-nag.sh)
            echo "/usr/local/bin/$file"
            ;;
        *)
            return 1
            ;;
    esac
}

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

    storage_count=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.storages | length" "$HOSTS_FILE")
    job_count=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs | length" "$HOSTS_FILE")

    if [[ "$storage_count" == "null" ]]; then
        storage_count="0"
    fi
    if [[ "$job_count" == "null" ]]; then
        job_count="0"
    fi

    if [[ "$storage_count" == "0" && "$job_count" == "0" ]]; then
        return 0
    fi

    {
        printf 'STORAGE_COUNT=%q\n' "$storage_count"
    } > "$storage_plan_file"

    for (( index=0; index<storage_count; index++ )); do
        local name server datastore username fingerprint password_var_name normalized_name

        name=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.storages[$index].name" "$HOSTS_FILE")
        server=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.storages[$index].server" "$HOSTS_FILE")
        datastore=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.storages[$index].datastore" "$HOSTS_FILE")
        username=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.storages[$index].username" "$HOSTS_FILE")
        fingerprint=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.storages[$index].fingerprint" "$HOSTS_FILE")

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

    {
        printf 'JOB_COUNT=%q\n' "$job_count"
    } > "$jobs_plan_file"

    for (( index=0; index<job_count; index++ )); do
        local schedule storage vmid compress mode notes_template notification_mode prune_backups enabled fleecing

        schedule=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].schedule" "$HOSTS_FILE")
        storage=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].storage" "$HOSTS_FILE")
        vmid=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].vmid // \"\"" "$HOSTS_FILE")
        compress=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].compress // \"zstd\"" "$HOSTS_FILE")
        mode=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].mode // \"snapshot\"" "$HOSTS_FILE")
        notes_template=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].notes_template // \"{{guestname}}\"" "$HOSTS_FILE")
        notification_mode=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].notification_mode // \"notification-system\"" "$HOSTS_FILE")
        prune_backups=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].prune_backups // \"keep-all=1\"" "$HOSTS_FILE")
        enabled=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].enabled // \"1\"" "$HOSTS_FILE")
        fleecing=$(yq e ".\"$host\".features.pve-postinstall.backup.standalone.jobs[$index].fleecing // \"0\"" "$HOSTS_FILE")

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
    local schedule
    local repository
    local backup_id
    local archive_name
    local ceph_enabled="false"
    local pbs_env_source
    local secret_profile

    repository=$(yq e ".\"$host\".features.pve-postinstall.backup.cluster.repository // \"\"" "$HOSTS_FILE")
    if [[ -z "$repository" || "$repository" == "null" ]]; then
        return 0
    fi

    schedule=$(yq e ".\"$host\".features.pve-postinstall.backup.cluster.schedule // \"00:30\"" "$HOSTS_FILE")
    backup_id=$(yq e ".\"$host\".features.pve-postinstall.backup.cluster.backup_id // \"pve-cluster-config\"" "$HOSTS_FILE")
    archive_name=$(yq e ".\"$host\".features.pve-postinstall.backup.cluster.archive_name // \"etc-pve\"" "$HOSTS_FILE")

    if hosts has "$host" "ceph"; then
        ceph_enabled="true"
    fi

    secret_profile=$(yq e ".\"$host\".features.pve-postinstall.backup.cluster.secret_profile // \"\"" "$HOSTS_FILE")
    case "$secret_profile" in
        backup-main)
            pbs_env_source="$PBS_ENV_BACKUP_MAIN"
            ;;
        backup-cinci)
            pbs_env_source="$PBS_ENV_BACKUP_CINCI"
            ;;
        "")
            pbs_env_source="$PBS_ENV_DIR_LEGACY/$host.env"
            if [[ ! -f "$pbs_env_source" && -f "$PBS_ENV_LEGACY_SOURCE" ]]; then
                pbs_env_source="$PBS_ENV_LEGACY_SOURCE"
            fi
            ;;
        *)
            print_warn "Invalid secret profile '$secret_profile' for $host"
            print_warn "Expected: backup-main or backup-cinci"
            return 1
            ;;
    esac

    if [[ ! -f "$pbs_env_source" ]]; then
        if [[ -n "$secret_profile" ]]; then
            print_warn "Missing secret file: $pbs_env_source"
            print_warn "Create it under: $SECRETS_DIR"
        else
            print_warn "Missing secret file: $PBS_ENV_DIR_LEGACY/$host.env"
            print_warn "Create it from: $PBS_ENV_DIR_LEGACY/.env.example"
            print_warn "or set backup.cluster.secret_profile in hosts.conf"
        fi
        return 1
    fi

    render_template "$CONFIGS_DIR/pve-config-backup.sh.tpl" "$build_dir/pve-config-backup.sh" \
        REPOSITORY="$repository" \
        BACKUP_ID="$backup_id" \
        ARCHIVE_NAME="$archive_name" \
        CEPH_ENABLED="$ceph_enabled"
    chmod 700 "$build_dir/pve-config-backup.sh"

    cat > "$build_dir/pve-config-backup.service" <<EOF
[Unit]
Description=PVE cluster config backup to PBS
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/homelab/pve-config-backup.env
ExecStart=/root/pve-config-backup.sh
EOF

    cat > "$build_dir/pve-config-backup.timer" <<EOF
[Unit]
Description=Daily PVE cluster config backup

[Timer]
OnCalendar=*-*-* $schedule
RandomizedDelaySec=120
Persistent=true
Unit=pve-config-backup.service

[Install]
WantedBy=timers.target
EOF

    cp "$pbs_env_source" "$build_dir/pbs.env"

    {
        printf 'REPOSITORY=%q\n' "$repository"
        printf 'BACKUP_ID=%q\n' "$backup_id"
        printf 'ARCHIVE_NAME=%q\n' "$archive_name"
        printf 'CEPH_ENABLED=%q\n' "$ceph_enabled"
    } > "$build_dir/restore-plan.conf"
}

build_notifications_bundle() {
    local host="$1"
    local build_dir="$2"
    local notifications_enabled
    local token_b64
    local chatid_b64
    local env_file="$TELEGRAM_ENV_SOURCE"

    notifications_enabled=$(yq e ".\"$host\".features.pve-postinstall.notifications // false" "$HOSTS_FILE")
    if [[ "$notifications_enabled" != "true" ]]; then
        return 0
    fi

    if [[ ! -f "$env_file" && -f "$TELEGRAM_ENV_FALLBACK" ]]; then
        env_file="$TELEGRAM_ENV_FALLBACK"
    fi

    if [[ ! -f "$env_file" ]]; then
        print_warn "telegram env file not found for notifications"
        print_warn "Create $TELEGRAM_ENV_SOURCE from secrets/telegram.env.example"
        print_warn "or provide $TELEGRAM_ENV_FALLBACK"
        return 1
    fi

    # shellcheck source=/dev/null
    source "$env_file"

    if [[ -z "${TELEGRAM_TOKEN:-}" || -z "${TELEGRAM_CHATID:-}" ]]; then
        print_warn "TELEGRAM_TOKEN and TELEGRAM_CHATID must be set in $env_file"
        return 1
    fi

    if [[ ! -f "$CONFIGS_DIR/notifications.cfg" ]]; then
        print_warn "Missing notifications config: $CONFIGS_DIR/notifications.cfg"
        return 1
    fi

    token_b64=$(printf '%s' "$TELEGRAM_TOKEN" | base64 | tr -d '\n')
    chatid_b64=$(printf '%s' "$TELEGRAM_CHATID" | base64 | tr -d '\n')

    cp "$CONFIGS_DIR/notifications.cfg" "$build_dir/notifications.cfg"

    cat > "$build_dir/priv-notifications.cfg" <<EOF
webhook: Telegram
	secret name=bot_id,value=${token_b64}
	secret name=chat_id,value=${chatid_b64}
EOF
}

build_network_interfaces_bundle() {
    local host="$1"
    local build_dir="$2"
    local interfaces_config
    local mgmt_ip
    local storage_ip
    local gateway

    interfaces_config=$(yq e ".\"$host\".features.pve-postinstall.interfaces // \"\"" "$HOSTS_FILE")
    if [[ -z "$interfaces_config" || "$interfaces_config" == "null" ]]; then
        return 0
    fi

    if [[ ! -f "$INTERFACES_TEMPLATE" ]]; then
        print_warn "Template not found: $INTERFACES_TEMPLATE"
        return 1
    fi

    mgmt_ip=$(yq e ".\"$host\".features.pve-postinstall.interfaces.mgmt_ip // \"\"" "$HOSTS_FILE")
    gateway=$(yq e ".\"$host\".features.pve-postinstall.interfaces.gateway // \"\"" "$HOSTS_FILE")
    storage_ip=$(yq e ".\"$host\".features.pve-postinstall.interfaces.storage_ip // \"\"" "$HOSTS_FILE")

    if [[ -z "$mgmt_ip" || -z "$gateway" || -z "$storage_ip" ]]; then
        print_warn "pve-postinstall.interfaces.{mgmt_ip,gateway,storage_ip} required for $host"
        return 1
    fi

    render_template "$INTERFACES_TEMPLATE" "$build_dir/interfaces" \
        NET_MGMT_IP="$mgmt_ip" NET_GATEWAY="$gateway" NET_STORAGE_IP="$storage_ip"
}

parse_common_flags "$@"
set -- "${PARSED_ARGS[@]}"

read -r -a SUPPORTED_HOSTS <<< "$(hosts list --feature pve-postinstall)"
if ! HOSTS=$(filter_hosts "${1:-all}" "${SUPPORTED_HOSTS[@]}"); then
    print_action "Skipping pve-postinstall (not applicable to $1)"
    exit 0
fi

deploy() {
    local host="$1"
    local host_type
    local timezone
    local ceph_enabled="false"
    local config_dir
    local -a files
    local build_dir="$BUILD_ROOT/$host"

    host_type=$(hosts get "$host" "type") || { print_warn "type missing for $host"; return 1; }
    timezone=$(hosts get "$host" "pve-postinstall.timezone" "UTC")
    case "$host_type" in
        pve)
            config_dir="$PVE_CONFIG_DIR"
            files=("${PVE_FILES[@]}")
            if hosts has "$host" "ceph"; then
                ceph_enabled="true"
            fi
            ;;
        *)
            print_warn "Unsupported host type for $host: $host_type"
            return 1
            ;;
    esac

    if [[ ! -d "$config_dir" ]]; then
        print_warn "Config directory missing: $config_dir"
        return 1
    fi

    prepare_build_dir "$build_dir"

    for file in "${files[@]}"; do
        if [[ ! -f "$config_dir/$file" ]]; then
            print_warn "Missing config file: $config_dir/$file"
            return 1
        fi
        cp "$config_dir/$file" "$build_dir/$file"
    done

    if ! build_standalone_backup_plans "$host" "$build_dir"; then
        return 1
    fi

    if ! build_cluster_config_backup_bundle "$host" "$build_dir"; then
        return 1
    fi

    if ! build_notifications_bundle "$host" "$build_dir"; then
        return 1
    fi

    if ! build_network_interfaces_bundle "$host" "$build_dir"; then
        return 1
    fi

    print_sub "Comparing with remote configs..."
    for file in "${files[@]}"; do
        local remote_path
        remote_path=$(remote_path_for_file "$file") || { print_warn "No remote path mapping for $file"; return 1; }
        diff_remote_config "$host" "$build_dir/$file" "$remote_path" || true
    done

    if [[ -f "$build_dir/notifications.cfg" ]]; then
        diff_remote_config "$host" "$build_dir/notifications.cfg" "/etc/pve/notifications.cfg" || true
        diff_remote_config "$host" "$build_dir/priv-notifications.cfg" "/etc/pve/priv/notifications.cfg" || true
    fi

    if [[ -f "$build_dir/interfaces" ]]; then
        diff_remote_config "$host" "$build_dir/interfaces" "/etc/network/interfaces" || true
    fi

    if [[ "$DRY_RUN" == true ]]; then
        print_sub "[DRY-RUN] Would deploy to $host:/tmp/homelab-pve-postinstall/"
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

        if [[ -f "$build_dir/notifications.cfg" ]]; then
            print_sub "Notifications subfeature: enabled"
        else
            print_sub "Notifications subfeature: disabled"
        fi

        if [[ -f "$build_dir/interfaces" ]]; then
            print_sub "Network interfaces subfeature: enabled"
        else
            print_sub "Network interfaces subfeature: disabled"
        fi
        return 0
    fi

    print_sub "Staging bundle..."
    ssh "$host" "rm -rf /tmp/homelab-pve-postinstall && mkdir -p /tmp/homelab-pve-postinstall/build /tmp/homelab-pve-postinstall/lib"
    scp -rq "$build_dir" "$host:/tmp/homelab-pve-postinstall/build/"
    scp -rq "$SCRIPT_DIR/scripts" "$host:/tmp/homelab-pve-postinstall/"
    scp -q "$HOMELAB_ROOT/lib/print.sh" "$HOMELAB_ROOT/lib/utils.sh" "$host:/tmp/homelab-pve-postinstall/lib/"

    print_sub "Running installer..."
    ssh "$host" "cd /tmp/homelab-pve-postinstall && chmod +x scripts/install.sh && if [ \"\$(id -u)\" -ne 0 ]; then echo 'Error: PVE deploy requires root SSH user' >&2; exit 1; fi && FORCE_UPDATE='$FORCE_UPDATE' ./scripts/install.sh '$host' '$host_type' '$timezone' '$ceph_enabled'"
}

deploy_init "PVE Post-Install Configs"
deploy_run deploy $HOSTS
deploy_finish

echo ""
echo "Apply changes:"
echo "  ssh <node> ifreload -a   # Apply without reboot (may disrupt connections)"
echo "  ssh <node> reboot        # Or reboot to apply safely"
