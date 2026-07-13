#!/bin/bash
# install-pbs-storage.sh - Install PVE standalone PBS storage definitions

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
PLAN_FILE="$BUILD_DIR/storage-plan.conf"
TOKENS_FILE="/etc/homelab/pbs-tokens.env"
STAGED_TOKENS_FILE="$BUILD_DIR/pbs-tokens.env"
STATE_DIR="/run/homelab-pve-backup"
STATE_FILE="$STATE_DIR/backup-state.env"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_file "$PLAN_FILE" "$PLAN_FILE" || exit 1

# shellcheck disable=SC1090
source "$PLAN_FILE"

if [[ -z "${STORAGE_COUNT:-}" ]]; then
    print_error "STORAGE_COUNT missing in $PLAN_FILE"
    exit 1
fi

cleanup_staged_tokens() {
    if [[ -f "$STAGED_TOKENS_FILE" ]]; then
        if command -v shred >/dev/null 2>&1; then
            shred -u -n 1 "$STAGED_TOKENS_FILE"
        else
            rm -f "$STAGED_TOKENS_FILE"
        fi
    fi
}
trap cleanup_staged_tokens EXIT

if [[ -f "$TOKENS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$TOKENS_FILE"
fi
if [[ -f "$STAGED_TOKENS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$STAGED_TOKENS_FILE"
fi

mkdir -p "$STATE_DIR"
storage_created="false"

storage_defined() {
    local name="$1"
    awk -v name="$name" '$1 == "pbs:" && $2 == name { found = 1 } END { exit found ? 0 : 1 }' /etc/pve/storage.cfg 2>/dev/null
}

# Print the full `pbs: <name>` stanza from storage.cfg, so a failed recreate can put
# the previous definition back instead of leaving the storage undefined.
storage_stanza() {
    local name="$1"
    awk -v name="$name" '
        $1 == "pbs:" && $2 == name { inside = 1; print; next }
        inside && /^[^[:space:]]/ { inside = 0 }
        inside { print }
    ' /etc/pve/storage.cfg 2>/dev/null
}

# `server` and `datastore` are FIXED properties in PVE's PBS storage plugin: passing
# either to `pvesm set` fails with "can't change value of fixed parameter" even when the
# value is unchanged. So they must be excluded from the converge path, and a change to
# either is the only thing that genuinely requires the destructive remove+add path.
storage_field() {
    local name="$1"
    local field="$2"
    storage_stanza "$name" | awk -v field="$field" '$1 == field { print $2; exit }'
}

pbs_add_is_transient_failure() {
    local error_file="$1"
    grep -Eiq "(Can't connect|Connection timed out|No route to host|Network is unreachable|Connection refused|Temporary failure|Name or service not known|could not resolve)" "$error_file"
}

for (( i=0; i<STORAGE_COUNT; i++ )); do
    name_var="STORAGE_${i}_NAME"
    server_var="STORAGE_${i}_SERVER"
    datastore_var="STORAGE_${i}_DATASTORE"
    namespace_var="STORAGE_${i}_NAMESPACE"
    username_var="STORAGE_${i}_USERNAME"
    fingerprint_var="STORAGE_${i}_FINGERPRINT"
    password_var_ref="STORAGE_${i}_PASSWORD_VAR"

    name="${!name_var}"
    server="${!server_var}"
    datastore="${!datastore_var}"
    namespace="${!namespace_var:-}"
    username="${!username_var}"
    fingerprint="${!fingerprint_var}"
    password_var_name="${!password_var_ref}"

    if [[ -z "$password_var_name" ]]; then
        print_error "Password variable name missing for $name"
        exit 1
    fi

    password="${!password_var_name:-}"
    if [[ -z "$password" ]]; then
        print_error "Missing $password_var_name in staged pve-backup tokens or $TOKENS_FILE"
        print_sub "Run ./deploy pve-backup $HOST from riven so PBS storage tokens are staged from 1Password"
        exit 1
    fi

    stanza_backup=""
    pw_backup=""
    pw_file="/etc/pve/priv/storage/${name}.pw"

    if storage_defined "$name"; then
        current_datastore="$(storage_field "$name" datastore)"
        current_server="$(storage_field "$name" server)"

        if [[ "$current_datastore" == "$datastore" && "$current_server" == "$server" ]]; then
            # Converge in place. pvesm set is idempotent and, unlike remove+add, never
            # leaves the storage undefined — a removed PBS storage means backups stop
            # silently, which is exactly what the old remove-then-add could do whenever
            # the re-add failed. Only mutable properties may be passed here: --server and
            # --datastore are fixed and would be rejected outright.
            print_sub "Updating PBS storage $name..."
            set_args=(
                --username "$username"
                --fingerprint "$fingerprint"
                --password "$password"
                --content backup
                --prune-backups keep-all=1
            )
            if [[ -n "$namespace" ]]; then
                set_args+=(--namespace "$namespace")
            fi
            pvesm set "$name" "${set_args[@]}"
            continue
        fi

        # A fixed property changed, so this genuinely needs a recreate. Capture the
        # current definition and its password first so we can put it back if the add fails.
        print_sub "Recreating PBS storage $name (datastore ${current_datastore:-none} -> $datastore, server ${current_server:-none} -> $server)..."
        stanza_backup="$(mktemp)"
        storage_stanza "$name" > "$stanza_backup"
        if [[ -f "$pw_file" ]]; then
            pw_backup="$(mktemp)"
            cp "$pw_file" "$pw_backup"
        fi
        pvesm remove "$name"
    else
        print_sub "Adding PBS storage $name..."
        storage_created="true"
    fi

    add_error_file="$(mktemp)"
    add_args=(
        --server "$server"
        --datastore "$datastore"
        --username "$username"
        --fingerprint "$fingerprint"
        --password "$password"
        --content backup
    )
    if [[ -n "$namespace" ]]; then
        add_args+=(--namespace "$namespace")
    fi
    if ! pvesm add pbs "$name" \
        "${add_args[@]}" 2>"$add_error_file"; then
        cat "$add_error_file" >&2
        transient=false
        pbs_add_is_transient_failure "$add_error_file" && transient=true
        rm -f "$add_error_file"

        if [[ -n "$stanza_backup" ]]; then
            cat "$stanza_backup" >> /etc/pve/storage.cfg
            rm -f "$stanza_backup"
            if [[ -n "$pw_backup" ]]; then
                mkdir -p "$(dirname "$pw_file")"
                cp "$pw_backup" "$pw_file"
                chmod 600 "$pw_file"
                rm -f "$pw_backup"
            fi
            print_warn "Restored previous definition of PBS storage $name after failed re-add"
        fi

        if [[ "$transient" == true ]]; then
            print_warn "PBS storage $name is not reachable/configurable yet; skipping until next deploy"
            continue
        fi
        exit 1
    fi
    rm -f "$add_error_file"
    [[ -n "$stanza_backup" ]] && rm -f "$stanza_backup"
    [[ -n "$pw_backup" ]] && rm -f "$pw_backup"

    print_sub "Ensuring prune policy on $name..."
    pvesm set "$name" --prune-backups keep-all=1
done

printf 'PBS_STORAGE_CREATED=%q\n' "$storage_created" > "$STATE_FILE"
