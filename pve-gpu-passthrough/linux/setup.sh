#!/bin/bash
# Rebuilds solar's guest-side setup on a fresh Bazzite (nvidia-open) install.
# Run as the auto-login desktop user from a copy of this directory on solar:
#
#   rsync -a ~/homelab/pve-gpu-passthrough/linux/ freender@solar:solar-setup/
#   ssh freender@solar solar-setup/setup.sh base     # before Steam; reboot after
#   ssh freender@solar solar-setup/setup.sh vr       # after Steam, SteamVR and PS VR2 App
#   ssh freender@solar solar-setup/setup.sh metrics  # node_exporter + solar-metrics (base runs it too)
#   ssh freender@solar solar-setup/setup.sh check    # read-only verification
#
# Every step is idempotent. The patched BlueZ is a separate step: bluez-eagain/README.md.
# Full procedure: Obsidian "Solar - Installation".
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STEAM="$HOME/.local/share/Steam"
STEAMVR="$STEAM/steamapps/common/SteamVR"
PLUGIN="$STEAM/steamapps/common/PlayStation VR2 App/SteamVR_Plug-In"
DL="$HOME/vr-downloads"
KARGS=(nvidia-modeset.conceal_vrr_caps=1 bluetooth.disable_ertm=1)

# Pinned releases, verified against the files solar ran on 2026-11-15.
IGNITION_URL=https://github.com/BnuuySolutions/Ignition/releases/download/v1.1.0/Ignition-Linux-Windows.zip
IGNITION_SHA=2637bb32c3e9d4ce2f8d8af4f1eaa7f2affe4c4f544868988ba4fd7da9e0f7ec
PSVR2TK_URL=https://github.com/BnuuySolutions/PSVR2Toolkit/releases/download/v1.0.0-experimental-3/PSVR2TK-win64-Ignition.zip
PSVR2TK_SHA=306e6bf9465b9266233db69e0c2175056b8cbed5dba0024e78dd056dbb0c6f67
UNITYSETUP_URL=https://github.com/BnuuySolutions/PSVR2Toolkit.UnitySetup/releases/download/v1.1.0/PSVR2Toolkit.UnitySetup-Linux.zip
UNITYSETUP_SHA=5e223773e6615f71b9496393ae9e1d74df43cab2a30e67e1a1b389e4cc8ad7da
SVLF_URL=https://github.com/BnuuySolutions/SteamVRLinuxFixes/releases/download/v0.2.1/VK_LAYER_BNUUY_steamvr_linux_fixes.zip
SVLF_SHA=af649a5aa45b732aa29989c401d23d9088f013c105c1d94893d77192addb6d05
NODE_EXPORTER_VERSION=1.12.1
NODE_EXPORTER_URL=https://github.com/prometheus/node_exporter/releases/download/v$NODE_EXPORTER_VERSION/node_exporter-$NODE_EXPORTER_VERSION.linux-amd64.tar.gz
NODE_EXPORTER_SHA=b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63
TEXTFILE_DIR=/var/lib/node_exporter/textfile
BLUEZ_MARKER=/usr/local/libexec/bluetooth/bluetoothd.built-for
METRICS_UNITS=(node_exporter.service solar-metrics.service solar-metrics.timer)

say() { printf '\n== %s\n' "$*"; }
# check output is parsed by solar-metrics: the id is one token, the reason follows ": ".
ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s: %s\n' "$1" "$2"; FAILED=1; }

install_etc() { # <repo path under etc/> <mode>
  local src="$HERE/etc/$1" dst="/etc/$1"
  if ! sudo cmp -s "$src" "$dst" 2>/dev/null; then
    sudo install -D -m "$2" "$src" "$dst"
    echo "  installed $dst"
  fi
}

fetch() { # <url> <sha256>
  local f
  f="$DL/$(basename "$1")"
  mkdir -p "$DL"
  [ -f "$f" ] || curl -fL --progress-bar -o "$f" "$1"
  echo "$2  $f" | sha256sum -c --quiet
  echo "$f"
}

base() {
  say "hostname"
  [ "$(hostnamectl --static)" = solar ] || sudo hostnamectl hostname solar

  say "kernel arguments (reboot to apply)"
  for k in "${KARGS[@]}"; do
    rpm-ostree kargs | tr ' ' '\n' | grep -qx "$k" || sudo rpm-ostree kargs --append-if-missing="$k"
  done

  say "/etc: key-only sshd, auto-login, udev"
  install_etc ssh/sshd_config.d/10-key-only.conf 0644
  sudo sshd -t
  sudo systemctl enable --now sshd
  sudo systemctl reload sshd
  install_etc plasmalogin.conf.d/autologin.conf 0644
  install_etc udev/rules.d/70-xrhardware.rules 0644
  install_etc udev/rules.d/71-psvr2-steamvr.rules 0644
  sudo udevadm control --reload

  say "Steam library disk"
  if ! grep -q '^LABEL=steam ' /etc/fstab; then
    if [ -e /dev/disk/by-label/steam ]; then
      echo 'LABEL=steam /var/mnt/steam ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2' | sudo tee -a /etc/fstab >/dev/null
      sudo mkdir -p /var/mnt/steam
      sudo systemctl daemon-reload
      sudo mount /var/mnt/steam
    else
      echo "  no ext4 labelled 'steam' (scsi1): format it first, see the Obsidian guide" >&2
    fi
  fi
  if mountpoint -q /var/mnt/steam && [ ! -O /var/mnt/steam ]; then
    sudo chown "$USER:" /var/mnt/steam
  fi

  say "headset-on SteamVR session"
  install -D -m 0755 "$HERE/psvr2-steamvr" "$HOME/.local/bin/psvr2-steamvr"
  install -D -m 0755 "$HERE/psvr2-audio" "$HOME/.local/bin/psvr2-audio"
  install -D -m 0644 "$HERE/psvr2-steamvr.service" "$HOME/.config/systemd/user/psvr2-steamvr.service"
  systemctl --user daemon-reload

  say "KDE: no idle dim, screen-off or lock"
  kwriteconfig6 --notify --file powerdevilrc --group AC --group Display --key DimDisplayWhenIdle false
  kwriteconfig6 --notify --file powerdevilrc --group AC --group Display --key TurnOffDisplayWhenIdle false
  kwriteconfig6 --notify --file kscreenlockerrc --group Daemon --key Autolock false
  kwriteconfig6 --notify --file kscreenlockerrc --group Daemon --key LockOnResume false

  metrics
}

# node_exporter on :9100 (FedoraWorkstation zone already allows 1025-65535/tcp) plus
# the solar-metrics timer. Alert rules: vmalert-rules/configs/solar.yml.
metrics() {
  say "metrics: node_exporter $NODE_EXPORTER_VERSION + solar-metrics timer"
  if [ "$(/usr/local/bin/node_exporter --version 2>/dev/null | awk 'NR==1 {print $3}')" != "$NODE_EXPORTER_VERSION" ]; then
    local z
    z=$(fetch "$NODE_EXPORTER_URL" "$NODE_EXPORTER_SHA")
    tar -xzf "$z" -C "$DL" --strip-components=1 "node_exporter-$NODE_EXPORTER_VERSION.linux-amd64/node_exporter"
    sudo install -m 0755 "$DL/node_exporter" /usr/local/bin/node_exporter
    sudo restorecon /usr/local/bin/node_exporter
    echo "  installed /usr/local/bin/node_exporter"
  fi
  if ! cmp -s "$HERE/solar-metrics" /usr/local/libexec/solar-metrics; then
    sudo install -D -m 0755 "$HERE/solar-metrics" /usr/local/libexec/solar-metrics
    sudo restorecon /usr/local/libexec/solar-metrics
    echo "  installed /usr/local/libexec/solar-metrics"
  fi
  sudo install -d -m 0755 -o "$USER" -g "$USER" "$TEXTFILE_DIR"
  for u in "${METRICS_UNITS[@]}"; do install_etc "systemd/system/$u" 0644; done
  sudo systemctl daemon-reload
  sudo systemctl enable --now node_exporter.service solar-metrics.timer
  sudo systemctl try-restart node_exporter.service
  sudo systemctl start solar-metrics.service
}

vr() {
  [ -x "$STEAMVR/bin/linux64/vrcompositor-launcher" ] || { echo "SteamVR (250820) is not installed" >&2; exit 1; }
  [ -f "$PLUGIN/driver.vrdrivermanifest" ] || { echo "PlayStation VR2 App (2580190) is not installed" >&2; exit 1; }
  if pgrep -x vrserver >/dev/null; then
    echo "SteamVR is running: quit it first (psvr2-steamvr quit)" >&2
    exit 1
  fi
  local z

  say "SteamVRLinuxFixes layer"
  z=$(fetch "$SVLF_URL" "$SVLF_SHA")
  rm -rf "$DL/svrfixes" && mkdir -p "$DL/svrfixes"
  unzip -oq "$z" -d "$DL/svrfixes"
  bash "$DL/svrfixes/install.sh"

  say "Ignition"
  z=$(fetch "$IGNITION_URL" "$IGNITION_SHA")
  mkdir -p "$HOME/ignition"
  unzip -oq "$z" -d "$HOME/ignition"
  chmod +x "$HOME"/ignition/*.sh "$HOME/ignition/proton"
  "$HOME/ignition/install_ignition.sh" "$PLUGIN/"
  chmod +x "$PLUGIN"/bin/linux64/*.sh "$PLUGIN/bin/linux64/proton"
  "$PLUGIN/bin/linux64/driver_install.sh"

  say "PSVR2Toolkit (Sony's dll kept as driver_playstation_vr2_orig.dll)"
  z=$(fetch "$PSVR2TK_URL" "$PSVR2TK_SHA")
  local win64="$PLUGIN/bin/win64"
  if [ ! -f "$win64/driver_playstation_vr2_orig.dll" ]; then
    mv "$win64/driver_playstation_vr2.dll" "$win64/driver_playstation_vr2_orig.dll"
  fi
  unzip -oq "$z" -d "$win64"

  say "PSVR2Toolkit.UnitySetup (play area tool)"
  z=$(fetch "$UNITYSETUP_URL" "$UNITYSETUP_SHA")
  mkdir -p "$HOME/psvr2-unitysetup"
  unzip -oq "$z" -d "$HOME/psvr2-unitysetup"
  chmod +x "$HOME/psvr2-unitysetup/PSVR2Toolkit.UnitySetup.x86_64"

  say "vrcompositor-launcher cap_sys_nice"
  sudo setcap CAP_SYS_NICE=eip "$STEAMVR/bin/linux64/vrcompositor-launcher"

  say "vrmonitor:// and steamvr:// handlers"
  if [ -f "$HOME/.local/share/applications/valve-vrmonitor.desktop" ]; then
    update-desktop-database "$HOME/.local/share/applications"
    xdg-mime default valve-vrmonitor.desktop x-scheme-handler/vrmonitor
    xdg-mime default valve-steamvr.desktop x-scheme-handler/steamvr
  else
    echo "  valve-*.desktop missing: start SteamVR once, then re-run 'setup.sh vr'" >&2
  fi

  say "OpenXR runtime: SteamVR"
  mkdir -p "$HOME/.config/openxr/1"
  ln -sfn "$STEAMVR/steamxr_linux64.json" "$HOME/.config/openxr/1/active_runtime.json"

  say "steamvr.vrsettings"
  "$HERE/steamvr-settings.sh"
}

check() {
  FAILED=0
  local cmdline
  cmdline=$(cat /proc/cmdline)
  [ "$(hostnamectl --static)" = solar ] && ok hostname || bad hostname "is '$(hostnamectl --static)'"
  for k in "${KARGS[@]}"; do
    [[ " $cmdline " == *" $k "* ]] && ok "karg:$k" || bad "karg:$k" "not booted"
  done
  [ "$(cat /sys/module/nvidia_modeset/parameters/conceal_vrr_caps 2>/dev/null)" = Y ] \
    && ok conceal_vrr_caps || bad conceal_vrr_caps "not Y"
  for f in ssh/sshd_config.d/10-key-only.conf plasmalogin.conf.d/autologin.conf \
    udev/rules.d/70-xrhardware.rules udev/rules.d/71-psvr2-steamvr.rules \
    "${METRICS_UNITS[@]/#/systemd/system/}"; do
    sudo cmp -s "$HERE/etc/$f" "/etc/$f" && ok "/etc/$f" || bad "/etc/$f" "differs from repo"
  done
  for f in .local/bin/psvr2-steamvr:psvr2-steamvr .local/bin/psvr2-audio:psvr2-audio \
    .config/systemd/user/psvr2-steamvr.service:psvr2-steamvr.service; do
    cmp -s "$HERE/${f#*:}" "$HOME/${f%%:*}" && ok "home/${f%%:*}" || bad "home/${f%%:*}" "differs from repo"
  done
  mountpoint -q /var/mnt/steam && ok steam_library || bad steam_library "/var/mnt/steam not mounted"
  systemctl is-active -q sshd && ok sshd || bad sshd "not active"
  [ -f "$HOME/.local/lib/libsteamvr_linux_fixes.so" ] && ok steamvr_linux_fixes || bad steamvr_linux_fixes "layer missing"
  [ "$(readlink -f "$PLUGIN/bin/linux64/driver_playstation_vr2.so")" = "$HOME/ignition/libdriver_ignition.so" ] \
    && ok ignition_shim || bad ignition_shim "driver_playstation_vr2.so is not the Ignition shim"
  cmp -s "$PLUGIN/bin/win64/driver_playstation_vr2.dll" <(unzip -p "$DL/PSVR2TK-win64-Ignition.zip" driver_playstation_vr2.dll 2>/dev/null) \
    && ok psvr2toolkit_dll || bad psvr2toolkit_dll "driver_playstation_vr2.dll is not the pinned PSVR2Toolkit (Steam restored Sony's?)"
  grep -q "PlayStation VR2 App/SteamVR_Plug-In" "$HOME/.config/openvr/openvrpaths.vrpath" 2>/dev/null \
    && ok openvr_driver || bad openvr_driver "PS VR2 driver not in openvrpaths external_drivers"
  sudo getcap "$STEAMVR/bin/linux64/vrcompositor-launcher" | grep -q cap_sys_nice \
    && ok compositor_cap_sys_nice || bad compositor_cap_sys_nice "vrcompositor-launcher lost cap_sys_nice"
  [ "$(xdg-mime query default x-scheme-handler/vrmonitor 2>/dev/null)" = valve-vrmonitor.desktop ] \
    && ok vrmonitor_handler || bad vrmonitor_handler "no vrmonitor:// handler"
  [ "$(readlink "$HOME/.config/openxr/1/active_runtime.json")" = "$STEAMVR/steamxr_linux64.json" ] \
    && ok openxr_runtime || bad openxr_runtime "not SteamVR"
  jq -e '.steamvr | .enableLinuxVulkanAsync and .useFacetRenderer and (.showMirrorView == false)' \
    "$HOME/.steam/steam/config/steamvr.vrsettings" >/dev/null && ok steamvr_settings || bad steamvr_settings "values differ"
  systemctl show -p ExecStart bluetooth | grep -q /usr/local/libexec/bluetooth/bluetoothd \
    && ok patched_bluetoothd || bad patched_bluetoothd "stock bluetoothd (bluez-eagain not installed)"
  # A Bazzite update that ships a new terra-bluez leaves the old patched build running.
  local bluez built
  bluez=$(rpm -q terra-bluez 2>/dev/null) || bluez="terra-bluez not installed"
  built=$(cat "$BLUEZ_MARKER" 2>/dev/null) || built="unknown (no $BLUEZ_MARKER)"
  [ "$bluez" = "$built" ] && ok bluez_build_matches \
    || bad bluez_build_matches "image has $bluez, patched bluetoothd built for $built: rebuild (bluez-eagain/README.md)"
  [ "$(/usr/local/bin/node_exporter --version 2>/dev/null | awk 'NR==1 {print $3}')" = "$NODE_EXPORTER_VERSION" ] \
    && ok node_exporter_version || bad node_exporter_version "not $NODE_EXPORTER_VERSION"
  cmp -s "$HERE/solar-metrics" /usr/local/libexec/solar-metrics \
    && ok solar_metrics_script || bad solar_metrics_script "/usr/local/libexec/solar-metrics differs from repo"
  systemctl is-enabled -q solar-metrics.timer && ok solar_metrics_timer || bad solar_metrics_timer "not enabled"
  return "$FAILED"
}

case "${1:-}" in
  base) base ;;
  vr) vr ;;
  metrics) metrics ;;
  check) check ;;
  *) echo "usage: $0 base|vr|metrics|check" >&2; exit 2 ;;
esac
