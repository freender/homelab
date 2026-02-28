#!/bin/bash
# install.sh - Install PVE storage definitions

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLAN_FILE="$SCRIPT_DIR/build/storage-plan.conf"
TOKENS_FILE="/etc/homelab/pbs-tokens.env"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    print_sub() { echo "    $*"; }
    print_warn() { echo "    Warning: $*"; }
fi

if [[ ! -f "$PLAN_FILE" ]]; then
    echo "Error: Missing storage plan: $PLAN_FILE"
    exit 1
fi

# shellcheck disable=SC1090
source "$PLAN_FILE"

if [[ -z "${STORAGE_COUNT:-}" ]]; then
    echo "Error: STORAGE_COUNT missing in $PLAN_FILE"
    exit 1
fi

if [[ -f "$TOKENS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$TOKENS_FILE"
fi

for (( i=0; i<STORAGE_COUNT; i++ )); do
    name_var="STORAGE_${i}_NAME"
    server_var="STORAGE_${i}_SERVER"
    datastore_var="STORAGE_${i}_DATASTORE"
    username_var="STORAGE_${i}_USERNAME"
    fingerprint_var="STORAGE_${i}_FINGERPRINT"
    password_var_ref="STORAGE_${i}_PASSWORD_VAR"

    name="${!name_var}"
    server="${!server_var}"
    datastore="${!datastore_var}"
    username="${!username_var}"
    fingerprint="${!fingerprint_var}"
    password_var_name="${!password_var_ref}"

    if pvesm status --storage "$name" >/dev/null 2>&1; then
        print_sub "Storage $name already configured"
    else
        if [[ -z "$password_var_name" ]]; then
            echo "Error: Password variable name missing for $name"
            exit 1
        fi

        password="${!password_var_name:-}"
        if [[ -z "$password" ]]; then
            echo "Error: Missing $password_var_name in $TOKENS_FILE"
            echo "Create $TOKENS_FILE from pve-storage/configs/pbs-tokens.env.example"
            exit 1
        fi

        print_sub "Adding PBS storage $name..."
        pvesm add pbs "$name" \
            --server "$server" \
            --datastore "$datastore" \
            --username "$username" \
            --fingerprint "$fingerprint" \
            --password "$password" \
            --content backup
    fi

    print_sub "Ensuring prune policy on $name..."
    pvesm set "$name" --prune-backups keep-all=1
done
