#!/bin/bash
# Interim: the `vr` stage of solar's guest-side setup, until vr-guest ports it
# (freender/homelab-ops#401). base, metrics and check moved to the vr-guest module
# (`./deploy vr-guest solar`, #400); the read-only check is now
# /usr/local/libexec/solar-check on solar. Run as the auto-login desktop user:
#
#   rsync -a ~/homelab/pve-gpu-passthrough/linux/ freender@solar:solar-setup/
#   ssh freender@solar solar-setup/setup.sh vr       # after Steam, SteamVR and PS VR2 App
#
# Idempotent. Full procedure: Obsidian "Solar - Installation".
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STEAM="$HOME/.local/share/Steam"
STEAMVR="$STEAM/steamapps/common/SteamVR"
PLUGIN="$STEAM/steamapps/common/PlayStation VR2 App/SteamVR_Plug-In"
DL="$HOME/vr-downloads"

# Pinned releases, verified against the files solar ran on 2026-11-15.
IGNITION_URL=https://github.com/BnuuySolutions/Ignition/releases/download/v1.1.0/Ignition-Linux-Windows.zip
IGNITION_SHA=2637bb32c3e9d4ce2f8d8af4f1eaa7f2affe4c4f544868988ba4fd7da9e0f7ec
PSVR2TK_URL=https://github.com/BnuuySolutions/PSVR2Toolkit/releases/download/v1.0.0-experimental-3/PSVR2TK-win64-Ignition.zip
PSVR2TK_SHA=306e6bf9465b9266233db69e0c2175056b8cbed5dba0024e78dd056dbb0c6f67
UNITYSETUP_URL=https://github.com/BnuuySolutions/PSVR2Toolkit.UnitySetup/releases/download/v1.1.0/PSVR2Toolkit.UnitySetup-Linux.zip
UNITYSETUP_SHA=5e223773e6615f71b9496393ae9e1d74df43cab2a30e67e1a1b389e4cc8ad7da
SVLF_URL=https://github.com/BnuuySolutions/SteamVRLinuxFixes/releases/download/v0.2.1/VK_LAYER_BNUUY_steamvr_linux_fixes.zip
SVLF_SHA=af649a5aa45b732aa29989c401d23d9088f013c105c1d94893d77192addb6d05

say() { printf '\n== %s\n' "$*"; }

fetch() { # <url> <sha256>
  local f
  f="$DL/$(basename "$1")"
  mkdir -p "$DL"
  [ -f "$f" ] || curl -fL --progress-bar -o "$f" "$1"
  echo "$2  $f" | sha256sum -c --quiet
  echo "$f"
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

case "${1:-}" in
  vr) vr ;;
  *) echo "usage: $0 vr  (base/metrics/check: ./deploy vr-guest solar)" >&2; exit 2 ;;
esac
