#!/bin/bash
# Run with sudo on solar. Swaps in the patched bluetoothd (BlueZ EAGAIN fix) via a systemd drop-in.
# Undo with rollback.sh. The image's /usr/libexec/bluetooth/bluetoothd is never touched.
set -euo pipefail
cd "$(dirname "$0")"
BIN=/usr/local/libexec/bluetooth/bluetoothd
install -D -m0755 bluetoothd "$BIN"
semanage fcontext -a -t bluetooth_exec_t "$BIN" 2>/dev/null \
  || semanage fcontext -m -t bluetooth_exec_t "$BIN"
restorecon -v "$(realpath "$BIN")"
mkdir -p /etc/systemd/system/bluetooth.service.d
cat > /etc/systemd/system/bluetooth.service.d/50-eagain-patch.conf <<EOF
# Patched bluetoothd: drop output reports on EAGAIN instead of destroying the UHID device.
# bluez/bluez#2431, homelab-ops#389. Remove with rollback.sh.
[Service]
ExecStart=
ExecStart=$BIN
EOF
systemctl daemon-reload
systemctl restart bluetooth
systemctl --no-pager -l status bluetooth | head -5
