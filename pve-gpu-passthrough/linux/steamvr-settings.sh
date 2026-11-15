#!/bin/bash
# Run as the desktop user on solar with SteamVR quit (it rewrites the file on exit).
# Applies the settled steamvr.vrsettings values with jq; never edit the file by hand,
# a JSON syntax error makes SteamVR wipe it.
set -euo pipefail

SETTINGS="$HOME/.steam/steam/config/steamvr.vrsettings"

if pgrep -x vrserver >/dev/null; then
  echo "SteamVR is running: quit it first (psvr2-steamvr quit)" >&2
  exit 1
fi

[ -f "$SETTINGS" ] || echo '{}' >"$SETTINGS"
cp -a "$SETTINGS" "$SETTINGS.bak-$(date +%Y%m%d%H%M%S)"

tmp=$(mktemp)
jq '.steamvr += {
  enableLinuxVulkanAsync: true,
  useFacetRenderer: true,
  showMirrorView: false,
  supersampleManualOverride: true,
  supersampleScale: 1.18
}' "$SETTINGS" >"$tmp"
mv "$tmp" "$SETTINGS"
jq '.steamvr | {enableLinuxVulkanAsync, useFacetRenderer, showMirrorView, supersampleManualOverride, supersampleScale}' "$SETTINGS"
