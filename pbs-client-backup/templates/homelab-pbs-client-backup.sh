#!/bin/bash

set -euo pipefail

CONFIG_FILE="/etc/homelab/pbs-client-backup.conf"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Missing config: $CONFIG_FILE" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"
if [[ -z "${BACKUP_ID:-}" || -z "${BACKUP_TYPE:-}" ]]; then
    echo "Missing required PBS backup configuration" >&2
    exit 1
fi

latest_snapshot_path() {
    local dataset="$1"
    local mountpoint latest_snapshot snapshot_name snapshot_path

    mountpoint="$(zfs get -H -o value mountpoint "$dataset")"
    if [[ -z "$mountpoint" || "$mountpoint" == "-" || "$mountpoint" == "none" || "$mountpoint" == "legacy" ]]; then
        echo "Dataset $dataset does not have a usable mountpoint: $mountpoint" >&2
        return 1
    fi

    latest_snapshot="$(zfs list -H -d 1 -t snapshot -o name -S creation "$dataset" | head -n 1)"
    if [[ -z "$latest_snapshot" || "$latest_snapshot" != *@* ]]; then
        echo "No snapshots found for $dataset" >&2
        return 1
    fi

    snapshot_name="${latest_snapshot#*@}"
    snapshot_path="$mountpoint/.zfs/snapshot/$snapshot_name"
    if [[ ! -d "$snapshot_path" ]]; then
        echo "Snapshot path not accessible: $snapshot_path" >&2
        return 1
    fi

    printf '%s\n' "$snapshot_path"
}

backup_with_client() {
    local repository="$1"
    local env_file="$2"
    local archive_specs=("${HOST_ARCHIVE_SPECS[@]}")
    local exclude_args=("${PBS_EXCLUDE_ARGS[@]}")
    local namespace_args=()
    local crypt_args=()

    if ! command -v proxmox-backup-client >/dev/null 2>&1; then
        echo "proxmox-backup-client not found" >&2
        return 1
    fi
    if [[ -n "${NAMESPACE:-}" ]]; then
        namespace_args=(--ns "$NAMESPACE")
    fi

    if [[ "${ENCRYPT:-false}" == "true" ]]; then
        if [[ -z "${KEYFILE:-}" || ! -f "$KEYFILE" ]]; then
            echo "ENCRYPT=true but keyfile missing: ${KEYFILE:-<unset>}" >&2
            return 1
        fi
        crypt_args=(--keyfile "$KEYFILE" --crypt-mode encrypt)
        echo "Client-side encryption enabled (keyfile: $KEYFILE)"
    fi

    if [[ ! -f "$env_file" ]]; then
        echo "Missing destination env file: $env_file" >&2
        return 1
    fi
    unset PBS_PASSWORD PBS_TOKEN_SECRET PBS_FINGERPRINT
    # shellcheck source=/dev/null
    source "$env_file"
    if [[ -z "${PBS_PASSWORD:-}" && -z "${PBS_TOKEN_SECRET:-}" ]]; then
        echo "PBS_PASSWORD or PBS_TOKEN_SECRET is not set" >&2
        return 1
    fi
    if [[ "$repository" == *'!'* && -z "${PBS_TOKEN_SECRET:-}" ]]; then PBS_TOKEN_SECRET="$PBS_PASSWORD"; fi
    if [[ "$repository" == *'!'* && -z "${PBS_PASSWORD:-}" ]]; then PBS_PASSWORD="$PBS_TOKEN_SECRET"; fi
    export PBS_PASSWORD PBS_TOKEN_SECRET PBS_FINGERPRINT
    proxmox-backup-client backup "${archive_specs[@]}" \
        "${exclude_args[@]}" \
        --repository "$repository" \
        "${namespace_args[@]}" \
        "${crypt_args[@]}" \
        --backup-type "$BACKUP_TYPE" \
        --backup-id "$BACKUP_ID"
}

HOST_ARCHIVE_SPECS=()
PBS_EXCLUDE_ARGS=()

archive_count="${ARCHIVE_COUNT:-0}"
if [[ ! "$archive_count" =~ ^[0-9]+$ || "$archive_count" -eq 0 ]]; then
    echo "ARCHIVE_COUNT must be a positive integer" >&2
    exit 1
fi

for ((i = 0; i < archive_count; i++)); do
    name_var="ARCHIVE_${i}_NAME"
    dataset_var="ARCHIVE_${i}_DATASET"
    path_var="ARCHIVE_${i}_PATH"
    exclude_count_var="ARCHIVE_${i}_EXCLUDE_COUNT"
    name="${!name_var:-}"
    dataset="${!dataset_var:-}"
    path="${!path_var:-}"
    exclude_count="${!exclude_count_var:-0}"
    if [[ -z "$name" ]]; then
        echo "Archive $i is missing name" >&2
        exit 1
    fi
    if [[ -n "$dataset" && -n "$path" ]] || [[ -z "$dataset" && -z "$path" ]]; then
        echo "Archive $i must specify exactly one of dataset or path" >&2
        exit 1
    fi
    if [[ ! "$exclude_count" =~ ^[0-9]+$ ]]; then
        echo "Archive $i exclude count is invalid" >&2
        exit 1
    fi

    if [[ -n "$dataset" ]]; then
        source_path="$(latest_snapshot_path "$dataset")"
        echo "Using $dataset latest snapshot: $source_path"
    else
        source_path="$path"
        if [[ ! -e "$source_path" ]]; then
            echo "Archive path not found: $source_path" >&2
            exit 1
        fi
        echo "Using path archive $name: $source_path"
    fi
    HOST_ARCHIVE_SPECS+=("${name}.pxar:${source_path}")

    for ((j = 0; j < exclude_count; j++)); do
        exclude_var="ARCHIVE_${i}_EXCLUDE_${j}"
        exclude="${!exclude_var:-}"
        if [[ -n "$exclude" ]]; then
            PBS_EXCLUDE_ARGS+=(--exclude "$exclude")
        fi
    done
done

destination_count="${DESTINATION_COUNT:-0}"
if [[ ! "$destination_count" =~ ^[1-9][0-9]*$ ]]; then
    echo "DESTINATION_COUNT must be a positive integer" >&2
    exit 1
fi
failed=0
for ((i = 0; i < destination_count; i++)); do
    repository_var="DESTINATION_${i}_REPOSITORY"
    env_file_var="DESTINATION_${i}_ENV_FILE"
    repository="${!repository_var:-}"
    env_file="${!env_file_var:-}"
    if [[ -z "$repository" || -z "$env_file" ]] || ! backup_with_client "$repository" "$env_file"; then
        echo "PBS client backup failed: $BACKUP_ID -> ${repository:-<missing destination>}" >&2
        failed=1
    else
        echo "PBS client backup completed: $BACKUP_ID -> $repository"
    fi
done
exit "$failed"
