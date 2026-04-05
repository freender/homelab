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

    cat > "$target_file" <<EOF
# Pin ethernet interface to $preferred_name
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="$selected_mac", NAME="$preferred_name"
EOF
}
