#!/bin/bash
set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
PLAN_FILE="$BUILD_DIR/sdn-plan.conf"

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

if [[ -z "${ZONE:-}" || -z "${BRIDGE:-}" || -z "${NODES:-}" ]]; then
    print_error "SDN plan missing ZONE, BRIDGE, or NODES"
    exit 1
fi

if ! command -v pvesh >/dev/null 2>&1; then
    print_error "pvesh not found; this module must run on Proxmox VE"
    exit 1
fi

changed=false

print_sub "Reconciling SDN VLAN zone $ZONE on $BRIDGE..."
if pvesh get "/cluster/sdn/zones/$ZONE" >/dev/null 2>&1; then
    print_sub "Zone $ZONE already exists; skipping create"
else
    pvesh create /cluster/sdn/zones --zone "$ZONE" --type vlan --bridge "$BRIDGE" --nodes "$NODES" >/dev/null
    changed=true
fi

count="${VNET_COUNT:-0}"
for ((i = 0; i < count; i++)); do
    name_var="VNET_${i}_NAME"
    tag_var="VNET_${i}_TAG"
    alias_var="VNET_${i}_ALIAS"
    name="${!name_var:-}"
    tag="${!tag_var:-}"
    alias="${!alias_var:-}"

    if [[ -z "$name" || -z "$tag" ]]; then
        print_error "VNet entry $i missing name or tag"
        exit 1
    fi

    print_sub "Reconciling VNet $name tag $tag..."
    if pvesh get "/cluster/sdn/vnets/$name" >/dev/null 2>&1; then
        print_sub "VNet $name already exists; skipping create"
    else
        pvesh create /cluster/sdn/vnets --vnet "$name" --zone "$ZONE" --tag "$tag" --alias "$alias" >/dev/null
        changed=true
    fi
done

if [[ "$changed" == "true" ]]; then
    print_sub "Applying SDN config..."
    pvesh set /cluster/sdn
fi
