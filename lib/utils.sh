#!/bin/bash
# lib/utils.sh - Lightweight utilities for remote hosts
# Sources: print.sh
# No external dependencies (no yq, no SSH, no downloads)

UTILS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [[ -f "${UTILS_DIR}/print.sh" ]]; then
    source "${UTILS_DIR}/print.sh"
else
    print_header() { echo "=== $* ==="; }
    print_action() { echo "==> $*"; }
    print_sub()    { echo "    $*"; }
    print_ok()     { echo "    ✓ $*"; }
    print_warn()   { echo "    ✗ Warning: $*"; }
    print_error()  { echo "    ✗ Error: $*" >&2; }
fi

FORCE_UPDATE=${FORCE_UPDATE:-false}
BACKUP_KEEP_COUNT=${BACKUP_KEEP_COUNT:-3}

# Assert that env vars are set AND non-empty, and fail loudly when they are not.
#
# The installers source a generated build/env and then branch on flags like
# ENABLE_ZFS_SNAPSHOTS. ensure_timer_state treats anything != "true" as "disable", so
# a truncated or partially-rendered env file does not error — it silently *disables*
# snapshots, scrub and replication. Bare `set -e` cannot catch that (the var is simply
# empty, no command fails). Guard the flags explicitly instead.
#
# Usage: require_env ENABLE_ZFS_SNAPSHOTS ENABLE_ZFS_SCRUB
require_env() {
    local name
    local missing=()

    for name in "$@"; do
        if [[ -z "${!name+x}" || -z "${!name}" ]]; then
            missing+=("$name")
        fi
    done

    if (( ${#missing[@]} > 0 )); then
        print_error "missing or empty required env value(s): ${missing[*]}"
        print_sub "The generated env file is incomplete; refusing to run with an ambiguous config."
        return 1
    fi
}

load_file_map() {
    local map_file="${1:-${BUILD_DIR:-}/file-map.conf}"
    local filename remote_path mode

    if [[ -z "$map_file" ]]; then
        print_error "file map path is required"
        return 1
    fi

    require_file "$map_file" "$map_file" || return 1

    declare -g -a FILE_MAP_NAMES=()
    declare -g -A FILE_MAP_DEST=()
    declare -g -A FILE_MAP_MODE=()
    while IFS='|' read -r filename remote_path mode; do
        [[ -n "$filename" ]] || continue
        FILE_MAP_NAMES+=("$filename")
        FILE_MAP_DEST["$filename"]="$remote_path"
        FILE_MAP_MODE["$filename"]="${mode:-644}"
    done < "$map_file"
}

mapped_dest() {
    local name="$1"

    if [[ -z "${FILE_MAP_DEST[$name]+x}" ]]; then
        print_error "missing file-map entry: $name"
        return 1
    fi

    printf '%s\n' "${FILE_MAP_DEST[$name]}"
}

mapped_mode() {
    local name="$1"

    if [[ -z "${FILE_MAP_MODE[$name]+x}" ]]; then
        print_error "missing file-map mode: $name"
        return 1
    fi

    printf '%s\n' "${FILE_MAP_MODE[$name]:-644}"
}

install_build_file() {
    local name="$1"
    local build_dir="${2:-${BUILD_DIR:-}}"
    local rc=0

    if [[ -z "$build_dir" ]]; then
        print_error "BUILD_DIR is required for install_build_file"
        return 2
    fi

    install_if_changed "$build_dir/$name" "$(mapped_dest "$name")" "$(mapped_mode "$name")" "$(mapped_dest "$name")" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || return "$rc"
    return "$rc"
}

# Install a file-map entry, then run a validation command against the resulting
# system state. If validation fails, restore the previous contents (or remove the
# file entirely when it is new) so a bad config never survives the deploy.
#
# Use this for files that cannot be validated in isolation because they are merged
# into a wider config at load time (e.g. an sshd_config.d drop-in). When a file
# CAN be checked standalone (e.g. `visudo -cf <file>`), prefer validating the build
# file before installing it — that never lets a broken file touch the system at all.
#
# Usage: install_build_file_validated sshd-hardening.conf sshd -t
# Returns: 0 when changed and valid, 1 when unchanged, 2 on error or failed validation
install_build_file_validated() {
    local name="$1"
    shift

    local dest backup="" rc=0
    dest="$(mapped_dest "$name")" || return 2

    if [[ -f "$dest" ]]; then
        backup="$(mktemp)"
        cp "$dest" "$backup"
    fi

    install_build_file "$name" || rc=$?

    if [[ $rc -ne 0 ]]; then
        [[ -n "$backup" ]] && rm -f "$backup"
        return "$rc"
    fi

    if "$@"; then
        [[ -n "$backup" ]] && rm -f "$backup"
        print_ok "$name validated"
        return 0
    fi

    print_error "validation failed for $name; rolling back $dest"
    if [[ -n "$backup" ]]; then
        cp "$backup" "$dest"
        rm -f "$backup"
    else
        rm -f "$dest"
    fi
    return 2
}

install_file_map() {
    local build_dir="${1:-${BUILD_DIR:-}}"
    local changed=1
    local name
    local rc

    if [[ -z "$build_dir" ]]; then
        print_error "BUILD_DIR is required for install_file_map"
        return 2
    fi

    for name in "${FILE_MAP_NAMES[@]}"; do
        rc=0
        install_build_file "$name" "$build_dir" || rc=$?
        [[ $rc -eq 0 || $rc -eq 1 ]] || return "$rc"
        [[ $rc -eq 0 ]] && changed=0
    done

    return "$changed"
}

require_file() {
    local path="$1"
    local label="${2:-$path}"

    if [[ ! -f "$path" ]]; then
        print_error "missing file: $label"
        return 1
    fi
}

require_dir() {
    local path="$1"
    local label="${2:-$path}"

    if [[ ! -d "$path" ]]; then
        print_error "missing directory: $label"
        return 1
    fi
}

ensure_parent_dir() {
    local path="$1"

    mkdir -p "$(dirname "$path")"
}

prune_backup_history() {
    local path="$1"
    local keep_count="${2:-$BACKUP_KEEP_COUNT}"
    local backup
    local count=0

    if [[ ! "$keep_count" =~ ^[0-9]+$ ]]; then
        keep_count=3
    fi

    shopt -s nullglob
    local backups=("${path}.bak."*)
    shopt -u nullglob

    if (( ${#backups[@]} <= keep_count )); then
        return 0
    fi

    while IFS= read -r backup; do
        count=$((count + 1))
        if (( count > keep_count )); then
            rm -rf "$backup"
        fi
    done < <(printf '%s\n' "${backups[@]}" | sort -r)
}

# Backup a file or directory
# Usage: backup_config /etc/foo/bar.conf
# Creates: /etc/foo/bar.conf.bak.YYYYMMDDHHmmss
backup_config() {
    local path="$1"
    [[ -e "$path" ]] || return 0

    local backup
    backup="${path}.bak.$(date +%Y%m%d%H%M%S)"
    if [[ -d "$path" ]]; then
        cp -r "$path" "$backup"
    else
        cp "$path" "$backup"
    fi

    prune_backup_history "$path"
}

# Return success when destination file is missing or content differs
# Usage: if file_needs_update /tmp/new.conf /etc/app.conf; then ...; fi
file_needs_update() {
    local src="$1"
    local dst="$2"

    if [[ ! -f "$src" ]]; then
        print_error "source file not found: $src"
        return 2
    fi

    if [[ ! -f "$dst" ]]; then
        return 0
    fi

    if [[ "$FORCE_UPDATE" == "true" ]]; then
        return 0
    fi

    if cmp -s "$src" "$dst"; then
        return 1
    fi

    return 0
}

# Copy file only when destination differs or is missing
# Returns: 0 when changed, 1 when unchanged, 2 on error
# Usage: copy_if_changed source destination [label]
copy_if_changed() {
    local src="$1"
    local dst="$2"
    local label="${3:-$dst}"
    local rc

    file_needs_update "$src" "$dst"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        if ! cp "$src" "$dst"; then
            print_error "failed to write $label"
            return 2
        fi
        print_sub "Updated $label"
        return 0
    fi

    if [[ $rc -eq 1 ]]; then
        print_sub "$label unchanged; skipping update"
        return 1
    fi

    return "$rc"
}

# Backup destination and copy file only when destination differs or is missing
# Returns: 0 when changed, 1 when unchanged, 2 on error
# Usage: backup_and_copy_if_changed source destination [label]
backup_and_copy_if_changed() {
    local src="$1"
    local dst="$2"
    local label="${3:-$dst}"
    local rc

    file_needs_update "$src" "$dst"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        backup_config "$dst"
        if ! cp "$src" "$dst"; then
            print_error "failed to write $label"
            return 2
        fi
        print_sub "Updated $label"
        return 0
    fi

    if [[ $rc -eq 1 ]]; then
        print_sub "$label unchanged; skipping update"
        return 1
    fi

    return "$rc"
}

# Install file with mode only when destination differs or is missing.
# Returns: 0 when changed, 1 when unchanged, 2 on error
# Usage: install_if_changed source destination mode [label]
install_if_changed() {
    local src="$1"
    local dst="$2"
    local mode="$3"
    local label="${4:-$dst}"
    local rc

    file_needs_update "$src" "$dst"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        ensure_parent_dir "$dst"
        if ! install -m "$mode" "$src" "$dst"; then
            print_error "failed to install $label"
            return 2
        fi
        print_sub "Updated $label"
        return 0
    fi

    if [[ $rc -eq 1 ]]; then
        print_sub "$label unchanged; skipping update"
        chmod "$mode" "$dst"
        return 1
    fi

    return "$rc"
}

# Backup destination and install file with mode only when changed.
# Returns: 0 when changed, 1 when unchanged, 2 on error
# Usage: backup_and_install_if_changed source destination mode [label]
backup_and_install_if_changed() {
    local src="$1"
    local dst="$2"
    local mode="$3"
    local label="${4:-$dst}"
    local rc

    file_needs_update "$src" "$dst"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        ensure_parent_dir "$dst"
        backup_config "$dst"
        if ! install -m "$mode" "$src" "$dst"; then
            print_error "failed to install $label"
            return 2
        fi
        print_sub "Updated $label"
        return 0
    fi

    if [[ $rc -eq 1 ]]; then
        print_sub "$label unchanged; skipping update"
        chmod "$mode" "$dst"
        return 1
    fi

    return "$rc"
}

# Enable or disable a systemd timer based on a flag, restarting it if units changed.
# Usage: ensure_timer_state <timer> <"true"|"false"> <units_changed>
ensure_timer_state() {
    local timer="$1"
    local enabled_flag="$2"
    local units_changed="${3:-false}"

    if [[ "$enabled_flag" != "true" ]]; then
        if systemctl is-enabled --quiet "$timer" 2>/dev/null \
            || systemctl is-active --quiet "$timer" 2>/dev/null; then
            systemctl disable --now "$timer"
            print_ok "$timer disabled"
        else
            print_sub "$timer disabled by config"
        fi
        return
    fi

    if ! systemctl is-enabled --quiet "$timer" 2>/dev/null; then
        systemctl enable --now "$timer"
        print_ok "$timer enabled"
    elif [[ "$units_changed" == "true" ]]; then
        systemctl restart "$timer"
        print_ok "$timer restarted"
    else
        print_sub "$timer already enabled"
    fi
}

# Stop, disable, and remove a managed systemd unit if it exists. Also clears
# any failed record for the unit, so a unit retired while in a failed state
# does not linger in `systemctl --failed` and trip an alert forever.
#
# Returns 0 when something was actually retired (unit stopped, or unit file
# removed) and 1 when there was nothing to do, matching the 0=changed /
# 1=unchanged convention used by copy_if_changed and install_if_changed.
# Callers running under `set -e` must consume the status (`|| true`, an `if`,
# or assignment to a flag) or a no-op retirement will abort the script.
#
# Usage: retire_systemd_unit unit-name /etc/systemd/system/unit-name
retire_systemd_unit() {
    local unit="$1"
    local unit_path="$2"
    local changed=false

    if systemctl is-enabled --quiet "$unit" 2>/dev/null \
        || systemctl is-active --quiet "$unit" 2>/dev/null; then
        systemctl disable --now "$unit"
        changed=true
        print_sub "Retired $unit"
    fi
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    if [[ -e "$unit_path" ]]; then
        rm -f "$unit_path"
        changed=true
        print_sub "Removed $unit_path"
    fi
    if [[ "$changed" == "true" ]]; then
        systemctl daemon-reload
        return 0
    fi
    return 1
}

# Apply the shared module "paused" convention.
#
# When paused, stop and disable the given systemd units so the module's managed
# service does no work, while the module stays deployed and can be resumed by
# flipping the flag back. This is distinct from the host-level `deploy: false`
# targeting gate, which skips deployment entirely and never touches the units.
#
# Returns 0 when paused, 1 when not paused, and 2 if a unit could not be stopped.
# Callers that pass a variable flag must distinguish all three statuses.
homelab_apply_pause() {
    local paused="$1"
    shift

    if [[ "$paused" != "true" ]]; then
        return 1
    fi

    local unit
    print_action "Pausing"
    for unit in "$@"; do
        [[ -n "$unit" ]] || continue
        if systemctl is-active --quiet "$unit" 2>/dev/null \
            || systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            if systemctl disable --now "$unit"; then
                print_ok "$unit stopped and disabled"
            else
                print_error "failed to stop and disable $unit"
                return 2
            fi
        else
            print_sub "$unit already stopped"
        fi
    done

    return 0
}

# Reload systemd and clear stale "failed" records after a module reinstalls
# its units — the standard follow-up to install_file_map.
#
# Pass the caller's own changed flag; this helper owns the gate, so call it
# unguarded rather than wrapping it in another `if`. When the flag is not
# "true" it does nothing at all: no reload, no reset.
#
# The gate matters. An unconditional reset-failed on a unit that did not
# change would silently hide a real, ongoing failure from `systemctl
# --failed`-based alerting (see homelab-alerting). Tying it to "new content
# was just written" keeps the reset meaning "a fix was deployed", not "a
# deploy happened to run".
#
# Note this clears failure state without proving the fix works — the unit is
# moved from "known failed" to "unknown" until its next run. Where a verdict
# is needed immediately, follow this with an explicit `systemctl start` and
# check the result (zfs-automation's replication recovery does exactly that).
#
# Usage: homelab_reload_and_clear_failed "$changed" unit1.service [unit2.timer ...]
homelab_reload_and_clear_failed() {
    local changed="$1"
    shift

    [[ "$changed" == "true" ]] || return 0

    systemctl daemon-reload

    local unit
    for unit in "$@"; do
        [[ -n "$unit" ]] || continue
        systemctl reset-failed "$unit" 2>/dev/null || true
    done
}

# Recover units that are currently in a failed state, regardless of whether
# this deploy changed anything.
#
# This is the counterpart to homelab_reload_and_clear_failed, covering the case
# that helper deliberately does not: a unit that failed for a transient external
# reason (container registry rate limit, network blip) and whose files are
# therefore byte-identical on redeploy. A redeploy is an explicit operator
# action to make the host right, so it should un-wedge such a unit rather than
# leave it failed until its next timer fire.
#
# It does not clear failure state blindly. reset-failed also clears systemd's
# StartLimitBurst rate limiter — without which `start` is refused outright for a
# unit with Restart=on-failure that exhausted its burst — and the subsequent
# start decides the outcome: a transient fault recovers and the alert clears
# with evidence, while a persistent one fails again immediately and stays
# visible to failed-unit alerting. Healthy units are never touched.
#
# Only use for units that are cheap, idempotent, and safe to run off-schedule.
# Do NOT use it for expensive or side-effecting jobs (backups, dist-upgrades);
# leave those to their timer.
#
# Waits up to HOMELAB_RECOVER_TIMEOUT seconds (default 300) because a
# Type=oneshot start blocks until the job finishes and oneshot disables
# TimeoutStartSec by default. On timeout it stops waiting; the job keeps running
# and the unit's own result remains authoritative. Always returns 0 — a
# still-failing unit is reported and left failed for alerting to catch, rather
# than aborting an otherwise good deploy over an external outage.
#
# Usage: homelab_recover_failed_units unit1.service [unit2.service ...]
homelab_recover_failed_units() {
    local timeout_s="${HOMELAB_RECOVER_TIMEOUT:-300}"
    local unit

    for unit in "$@"; do
        [[ -n "$unit" ]] || continue
        systemctl is-failed --quiet "$unit" 2>/dev/null || continue

        print_action "Recovering failed $unit"
        systemctl reset-failed "$unit" 2>/dev/null || true
        if timeout "$timeout_s" systemctl start "$unit"; then
            print_ok "$unit recovered"
        elif systemctl is-failed --quiet "$unit" 2>/dev/null; then
            print_warn "$unit still failing after restart; left failed for alerting"
        else
            print_warn "$unit did not settle within ${timeout_s}s; job still running"
        fi
    done

    return 0
}

# Mask a systemd unit that should never run on this host (an LSB init script
# with no matching hardware, an unwanted distro default, etc.), and clear any
# stale failed record left over from before it was masked (or a spurious
# start attempt) so it does not trip a failed-unit alert.
#
# Idempotent and safe to call every run, including when the unit is not
# installed at all (returns 0 as a no-op).
#
# Usage: homelab_mask_unwanted_service unit.service "reason for masking"
homelab_mask_unwanted_service() {
    local unit="$1"
    local reason="${2:-}"

    if ! systemctl list-unit-files "$unit" >/dev/null 2>&1; then
        print_sub "$unit not installed; nothing to mask"
        return 0
    fi
    if [[ "$(systemctl is-enabled "$unit" 2>/dev/null)" == "masked" ]]; then
        systemctl reset-failed "$unit" >/dev/null 2>&1 || true
        print_sub "$unit already masked"
        return 0
    fi
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    systemctl mask "$unit"
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    if [[ -n "$reason" ]]; then
        print_ok "$unit masked ($reason)"
    else
        print_ok "$unit masked"
    fi
}
