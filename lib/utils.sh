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
        cp "$src" "$dst"
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
        cp "$src" "$dst"
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
        install -m "$mode" "$src" "$dst"
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
        install -m "$mode" "$src" "$dst"
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

# Sync files from source directory into destination directory when changed
# Returns: 0 if any file changed, 1 if no files changed, 2 on error
# Usage: sync_dir_if_changed /tmp/src.d /etc/app.d [label]
sync_dir_if_changed() {
    local src_dir="$1"
    local dst_dir="$2"
    local label="${3:-$dst_dir}"
    local changed=1
    local file

    if [[ ! -d "$src_dir" ]]; then
        print_error "source directory not found: $src_dir"
        return 2
    fi

    mkdir -p "$dst_dir"

    shopt -s nullglob
    for file in "$src_dir"/*; do
        local target_file
        target_file="$dst_dir/$(basename "$file")"
        if copy_if_changed "$file" "$target_file" "$label/$(basename "$file")"; then
            changed=0
        else
            local rc=$?
            if [[ $rc -ne 1 ]]; then
                shopt -u nullglob
                return "$rc"
            fi
        fi
    done
    shopt -u nullglob

    return "$changed"
}
