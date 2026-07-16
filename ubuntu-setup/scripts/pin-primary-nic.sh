#!/bin/bash

get_iface_mac() {
    local iface="$1"
    cat "/sys/class/net/$iface/address"
}

iface_is_ethernet() {
    local iface="$1"
    [[ -f "/sys/class/net/$iface/type" ]] && [[ "$(cat "/sys/class/net/$iface/type")" == "1" ]]
}

iface_has_carrier() {
    local iface="$1"
    [[ -f "/sys/class/net/$iface/carrier" ]] && [[ "$(cat "/sys/class/net/$iface/carrier")" == "1" ]]
}

select_fallback_iface() {
    local iface=""

    iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
    if [[ -n "$iface" ]] && iface_is_ethernet "$iface" && iface_has_carrier "$iface"; then
        printf '%s\n' "$iface"
        return 0
    fi

    for iface in /sys/class/net/*; do
        iface="$(basename "$iface")"
        [[ "$iface" == "lo" ]] && continue
        if iface_is_ethernet "$iface" && iface_has_carrier "$iface"; then
            printf '%s\n' "$iface"
            return 0
        fi
    done

    return 1
}

# Look for other on-host mechanisms that could also try to name/rename the interface
# we are about to pin, and warn about them. Best-effort and non-fatal: a false
# positive here must never block a deploy, so this only ever prints print_warn.
#
# Covers the naming sources this repo does not otherwise track: a legacy Debian
# 70-persistent-net.rules file, a second udev rule matching the same MAC, a netplan
# YAML (including cloud-init's auto-generated 50-cloud-init.yaml) referencing the
# same MAC, and an active NetworkManager that owns the same device.
warn_competing_nic_rules() {
    local target_file="$1"
    local selected_mac="$2"
    local rule_file np_file

    if [[ -f /etc/udev/rules.d/70-persistent-net.rules ]]; then
        print_warn "legacy /etc/udev/rules.d/70-persistent-net.rules present; may race with $target_file"
    fi

    shopt -s nullglob
    for rule_file in /etc/udev/rules.d/*.rules; do
        [[ "$rule_file" -ef "$target_file" ]] && continue
        if grep -qi "$selected_mac" "$rule_file" 2>/dev/null && grep -q 'NAME=' "$rule_file"; then
            print_warn "competing udev naming rule for $selected_mac in $rule_file"
        fi
    done

    if [[ -d /etc/netplan ]]; then
        for np_file in /etc/netplan/*.yaml; do
            if grep -qi "$selected_mac" "$np_file" 2>/dev/null; then
                print_warn "netplan config $np_file also references $selected_mac; verify it does not set a conflicting name (cloud-init can regenerate this file on next boot)"
            fi
        done
    fi
    shopt -u nullglob

    if command -v nmcli >/dev/null 2>&1 && systemctl is-active --quiet NetworkManager 2>/dev/null; then
        if nmcli -t -f GENERAL.HWADDR device show 2>/dev/null | grep -qi "$selected_mac"; then
            print_warn "NetworkManager is active and manages a device with MAC $selected_mac; it may rename/reconfigure this interface independently of udev"
        fi
    fi
}

write_primary_nic_rule() {
    local target_file="$1"
    local preferred_name="$2"
    local preferred_mac="$3"
    local selected_mac="$preferred_mac"
    local selected_iface=""

    if [[ -n "$preferred_mac" ]]; then
        if ! grep -qi "$preferred_mac" /sys/class/net/*/address 2>/dev/null; then
            selected_iface="$(select_fallback_iface)" || return 1
            selected_mac="$(get_iface_mac "$selected_iface")"
        fi
    else
        selected_iface="$(select_fallback_iface)" || return 1
        selected_mac="$(get_iface_mac "$selected_iface")"
    fi

    warn_competing_nic_rules "$target_file" "$selected_mac"

    cat > "$target_file" <<EOF
# Pin ethernet interface to $preferred_name
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="$selected_mac", NAME="$preferred_name"
EOF
}
