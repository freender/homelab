#!/bin/bash

set -euo pipefail

CONFIG_FILE="/etc/homelab/pbs-client-backup.conf"
ENV_FILE="/etc/homelab/pbs-client-backup.env"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Missing config: $CONFIG_FILE" >&2
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing env file: $ENV_FILE" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"
# shellcheck source=/dev/null
source "$ENV_FILE"

if [[ -z "${PBS_PASSWORD:-}" && -z "${PBS_TOKEN_SECRET:-}" ]]; then
    echo "PBS_PASSWORD or PBS_TOKEN_SECRET is not set" >&2
    exit 1
fi

if [[ "$REPOSITORY" == *'!'* && -z "${PBS_TOKEN_SECRET:-}" ]]; then
    PBS_TOKEN_SECRET="$PBS_PASSWORD"
fi
if [[ "$REPOSITORY" == *'!'* && -z "${PBS_PASSWORD:-}" ]]; then
    PBS_PASSWORD="$PBS_TOKEN_SECRET"
fi

export PBS_PASSWORD PBS_TOKEN_SECRET PBS_FINGERPRINT

if [[ -z "${REPOSITORY:-}" || -z "${BACKUP_ID:-}" || -z "${BACKUP_TYPE:-}" ]]; then
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

backup_with_host_client() {
    local archive_specs=("${HOST_ARCHIVE_SPECS[@]}")
    local exclude_args=("${PBS_EXCLUDE_ARGS[@]}")
    local namespace_args=()

    if ! command -v proxmox-backup-client >/dev/null 2>&1; then
        echo "proxmox-backup-client not found" >&2
        return 1
    fi
    if [[ -n "${NAMESPACE:-}" ]]; then
        namespace_args=(--ns "$NAMESPACE")
    fi

    proxmox-backup-client backup "${archive_specs[@]}" \
        "${exclude_args[@]}" \
        --repository "$REPOSITORY" \
        "${namespace_args[@]}" \
        --backup-type "$BACKUP_TYPE" \
        --backup-id "$BACKUP_ID"
}

backup_with_docker_client() {
    local archive_specs=("${DOCKER_ARCHIVE_SPECS[@]}")
    local add_host_args=("${DOCKER_ADD_HOST_ARGS[@]}")
    local docker_args=("${DOCKER_MOUNT_ARGS[@]}")
    local exclude_args=("${PBS_EXCLUDE_ARGS[@]}")
    local namespace_args=()

    if [[ -z "${DOCKER_IMAGE:-}" ]]; then
        echo "DOCKER_IMAGE is required for docker runner" >&2
        return 1
    fi
    if ! command -v docker >/dev/null 2>&1; then
        echo "docker not found" >&2
        return 1
    fi
    if [[ -n "${NAMESPACE:-}" ]]; then
        namespace_args=(--ns "$NAMESPACE")
    fi

    docker run --rm \
        --network "${DOCKER_NETWORK:-bridge}" \
        --env PBS_PASSWORD \
        --env PBS_TOKEN_SECRET \
        --env PBS_FINGERPRINT \
        "${add_host_args[@]}" \
        "${docker_args[@]}" \
        --entrypoint proxmox-backup-client \
        "$DOCKER_IMAGE" \
        backup "${archive_specs[@]}" \
            "${exclude_args[@]}" \
            --repository "$REPOSITORY" \
            "${namespace_args[@]}" \
            --backup-type "$BACKUP_TYPE" \
            --backup-id "$BACKUP_ID"
}

HOST_ARCHIVE_SPECS=()
DOCKER_ARCHIVE_SPECS=()
DOCKER_ADD_HOST_ARGS=()
DOCKER_MOUNT_ARGS=()
PBS_EXCLUDE_ARGS=()

docker_add_host_count="${DOCKER_ADD_HOST_COUNT:-0}"
if [[ ! "$docker_add_host_count" =~ ^[0-9]+$ ]]; then
    echo "DOCKER_ADD_HOST_COUNT must be a non-negative integer" >&2
    exit 1
fi
for ((i = 0; i < docker_add_host_count; i++)); do
    add_host_var="DOCKER_ADD_HOST_${i}"
    add_host="${!add_host_var:-}"
    if [[ -n "$add_host" ]]; then
        DOCKER_ADD_HOST_ARGS+=(--add-host "$add_host")
    fi
done

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
    DOCKER_ARCHIVE_SPECS+=("${name}.pxar:/backup-source/${name}")
    DOCKER_MOUNT_ARGS+=(--mount "type=bind,src=${source_path},dst=/backup-source/${name},readonly")

    for ((j = 0; j < exclude_count; j++)); do
        exclude_var="ARCHIVE_${i}_EXCLUDE_${j}"
        exclude="${!exclude_var:-}"
        if [[ -n "$exclude" ]]; then
            PBS_EXCLUDE_ARGS+=(--exclude "$exclude")
        fi
    done
done

case "${RUNNER:-host}" in
    host|native)
        backup_with_host_client
        ;;
    docker)
        backup_with_docker_client
        ;;
    *)
        echo "Unsupported RUNNER: ${RUNNER:-}" >&2
        exit 1
        ;;
esac

echo "PBS client backup completed: $BACKUP_ID -> $REPOSITORY"
