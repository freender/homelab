#!/bin/bash
# install.sh - Install GPU passthrough configs
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    }
    print_sub() { echo "    $*"; }
fi

if [[ ! -f "$BUILD_DIR/blacklist.conf" || ! -f "$BUILD_DIR/cmdline" || ! -f "$BUILD_DIR/vfio.conf" || ! -f "$BUILD_DIR/modules" ]]; then
    echo "Error: Missing build artifacts in $BUILD_DIR"
    exit 1
fi

if [[ ! -f /etc/kernel/cmdline ]]; then
    echo "Error: /etc/kernel/cmdline not found (systemd-boot required)"
    exit 1
fi

print_sub "Backing up configs..."
backup_config /etc/kernel/cmdline
backup_config /etc/modules

print_sub "Updating systemd-boot cmdline..."
cmdline=$(cat "$BUILD_DIR/cmdline")
current_cmdline=$(cat /etc/kernel/cmdline)
[[ "$current_cmdline" != *"root="* ]] && current_cmdline=$(cat /proc/cmdline)

filtered_args=()
seen_args=()
for arg in $current_cmdline; do
    case "$arg" in
        BOOT_IMAGE=*|initrd=*|intel_iommu=*|amd_iommu=*|iommu=*|pcie_acs_override=*|video=*)
            continue ;;
        *)
            already_seen=false
            for seen_arg in "${seen_args[@]}"; do
                if [[ "$seen_arg" == "$arg" ]]; then
                    already_seen=true
                    break
                fi
            done

            if [[ "$already_seen" == "false" ]]; then
                filtered_args+=("$arg")
                seen_args+=("$arg")
            fi
            ;;
    esac
done

cmdline_args=()
read -r -a cmdline_args <<< "$cmdline"
new_cmdline=("${filtered_args[@]}" "${cmdline_args[@]}")
final_args=()
final_seen=()
for arg in "${new_cmdline[@]}"; do
    already_seen=false
    for seen_arg in "${final_seen[@]}"; do
        if [[ "$seen_arg" == "$arg" ]]; then
            already_seen=true
            break
        fi
    done

    if [[ "$already_seen" == "false" ]]; then
        final_args+=("$arg")
        final_seen+=("$arg")
    fi
done
final="${final_args[*]}"

printf '%s\n' "$final" > /etc/kernel/cmdline
proxmox-boot-tool refresh

print_sub "Deploying modprobe configs..."
cp "$BUILD_DIR/blacklist.conf" /etc/modprobe.d/blacklist.conf
cp "$BUILD_DIR/vfio.conf" /etc/modprobe.d/vfio.conf

print_sub "Deploying VFIO modules..."
cp "$BUILD_DIR/modules" /etc/modules-load.d/vfio.conf

print_sub "Cleaning legacy /etc/modules..."
if [[ -f /etc/modules ]]; then
    grep -v '^vfio' /etc/modules > /tmp/modules.clean && mv /tmp/modules.clean /etc/modules
fi

print_sub "Deploying emergency removal script..."
if [[ -f "$SCRIPT_DIR/scripts/remove-local.sh" ]]; then
    cp "$SCRIPT_DIR/scripts/remove-local.sh" /root/pve-gpu-passthrough-remove.sh
    chmod +x /root/pve-gpu-passthrough-remove.sh
fi

print_sub "Updating initramfs..."
update-initramfs -u -k all
