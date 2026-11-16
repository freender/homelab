#!/bin/bash
# Run with sudo on solar. Returns bluetooth.service to the image's stock bluetoothd.
set -euo pipefail
BIN=/usr/local/libexec/bluetooth/bluetoothd
rm -f /etc/systemd/system/bluetooth.service.d/50-eagain-patch.conf
rmdir --ignore-fail-on-non-empty /etc/systemd/system/bluetooth.service.d
systemctl daemon-reload
systemctl restart bluetooth
semanage fcontext -d "$BIN" 2>/dev/null || true
rm -f "$BIN" "$BIN.built-for"
systemctl show -p ExecStart bluetooth | head -1
