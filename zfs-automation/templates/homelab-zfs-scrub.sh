#!/bin/bash

set -euo pipefail

# shellcheck disable=SC1054,SC1056,SC1072,SC1073,SC1083
{{ ZFS_POOLS_BLOCK }}
# shellcheck enable=SC1054,SC1056,SC1072,SC1073,SC1083
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-300}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-0}"

log() {
    printf '%s %s\n' "$(date +"%Y-%m-%d %H:%M:%S %Z")" "$*"
}

require_positive_integer() {
    local value="$1"
    local name="$2"

    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        printf '%s must be an integer, got: %s\n' "$name" "$value" >&2
        exit 1
    fi
}

extract_scan_line() {
    local status_output="$1"

    printf '%s\n' "$status_output" | grep -E '^[[:space:]]*scan:' || true
}

scrub_in_progress() {
    local scan_line="$1"

    [[ "$scan_line" == *"scrub in progress"* ]]
}

scrub_result_is_clean() {
    local status_output="$1"
    local scan_line="$2"
    local errors_line

    errors_line="$(printf '%s\n' "$status_output" | grep -E '^errors:' || true)"

    if [[ "$scan_line" == *"scrub canceled"* ]] || [[ "$scan_line" == *"scrub cancelled"* ]] || [[ "$scan_line" == *"scrub interrupted"* ]]; then
        log "Scrub did not complete cleanly: $scan_line"
        return 1
    fi

    if [[ "$scan_line" != *"scrub repaired 0B"* ]]; then
        log "Scrub repaired data or reported an unexpected result: $scan_line"
        return 1
    fi

    if [[ "$scan_line" != *"with 0 errors"* ]]; then
        log "Scrub reported data errors: $scan_line"
        return 1
    fi

    if [[ "$errors_line" != "errors: No known data errors" ]]; then
        log "Pool reported errors after scrub: ${errors_line:-errors line missing}"
        return 1
    fi

    return 0
}

require_positive_integer "$POLL_INTERVAL_SECONDS" "POLL_INTERVAL_SECONDS"
require_positive_integer "$MAX_WAIT_SECONDS" "MAX_WAIT_SECONDS"

scrub_pool() {
    local zfs_pool="$1"
    local start_output
    local current_status
    local current_scan_line
    local elapsed_seconds=0

    log "Starting scrub monitor for pool $zfs_pool"

    if ! start_output="$(/sbin/zpool scrub "$zfs_pool" 2>&1)"; then
        current_status="$(/sbin/zpool status "$zfs_pool")"
        current_scan_line="$(extract_scan_line "$current_status")"
        if scrub_in_progress "$current_scan_line"; then
            log "Scrub already in progress; attaching to existing run"
        else
            printf '%s\n' "$start_output" >&2
            exit 1
        fi
    else
        log "Scrub started for pool $zfs_pool"
    fi

    while true; do
        current_status="$(/sbin/zpool status "$zfs_pool")"
        current_scan_line="$(extract_scan_line "$current_status")"

        if [[ -z "$current_scan_line" ]]; then
            printf '%s\n' "$current_status"
            log "Missing scrub status line for pool $zfs_pool"
            exit 1
        fi

        log "$current_scan_line"

        if ! scrub_in_progress "$current_scan_line"; then
            break
        fi

        if (( MAX_WAIT_SECONDS > 0 && elapsed_seconds >= MAX_WAIT_SECONDS )); then
            printf '%s\n' "$current_status"
            log "Timed out waiting for scrub to finish after ${elapsed_seconds}s"
            exit 1
        fi

        sleep "$POLL_INTERVAL_SECONDS"
        elapsed_seconds=$((elapsed_seconds + POLL_INTERVAL_SECONDS))
    done

    printf '%s\n' "$current_status"

    if ! scrub_result_is_clean "$current_status" "$current_scan_line"; then
        exit 1
    fi

    log "Scrub completed cleanly for pool $zfs_pool"
}

for zfs_pool in "${ZFS_POOLS[@]}"; do
    scrub_pool "$zfs_pool"
done
