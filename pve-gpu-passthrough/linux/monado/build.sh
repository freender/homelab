#!/bin/bash
# Build Monado (Supremium psvr2-linux-steam-lh) + xrizer for solar's Envision profile
# "psvr2-toolkit-monado" (freender/homelab-ops#389). Runs inside the fedora:45 distrobox
# `vr-build` (deps: `dnf builddep monado`, rust, cargo, clang-devel). Run monado-service
# OUTSIDE the box. Paths match the Envision profile so Envision can start it without rebuilding.
# patches/ are from github.com/Villagers654/framework-16-linux-psvr2 (MIT): Sense haptics on
# the Touch profile, and the steamvr_lh pose-time unit fix plus play-area restore.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
ROOT=$HOME/.local/share/envision/psvr2-toolkit-monado
PREFIX=$HOME/.local/share/envision/prefixes/psvr2-toolkit-monado
SRC=$ROOT/xrservice
XRIZER=$ROOT/xrizer

for p in "$here"/patches/*.patch; do
  if git -C "$SRC" apply --check "$p" 2>/dev/null; then git -C "$SRC" apply "$p"; echo "applied $(basename "$p")"
  elif git -C "$SRC" apply --reverse --check "$p" 2>/dev/null; then echo "already applied $(basename "$p")"
  else echo "patch does not apply: $p" >&2; exit 1; fi
done

# PSVR2 + Sense come from the PSVR2Toolkit SteamVR driver via steamvr_lh (LH_LOAD_PSVR2=1),
# so Monado's native psvr2 and pssense drivers stay off and cannot claim the devices.
# `dnf builddep monado` also installs optional libs (OpenCV, RealSense, libsurvive, OpenHMD,
# libuvc, OpenVR) that Bazzite lacks; they stay off so monado-service runs on the host.
cmake -S "$SRC" -B "$SRC/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DXRT_FEATURE_OPENXR=ON -DXRT_FEATURE_SERVICE=ON \
  -DXRT_BUILD_DRIVER_STEAMVR_LIGHTHOUSE=ON \
  -DXRT_BUILD_DRIVER_PSVR2=OFF -DXRT_BUILD_DRIVER_PSSENSE=OFF \
  -DXRT_HAVE_OPENCV=OFF -DXRT_HAVE_REALSENSE=OFF -DXRT_HAVE_LIBUVC=OFF -DXRT_HAVE_OPENVR=OFF \
  -DXRT_BUILD_DRIVER_SURVIVE=OFF -DXRT_BUILD_DRIVER_OHMD=OFF
cmake --build "$SRC/build"
cmake --install "$SRC/build"
grep -q steamvr_lh_create_devices <(nm -C "$PREFIX/bin/monado-service") || { echo "steamvr_lh missing from monado-service" >&2; exit 1; }

missing=$(distrobox-host-exec ldd "$PREFIX/bin/monado-service" | grep 'not found' || true)
[ -z "$missing" ] || { echo "monado-service needs libs the host lacks:" >&2; echo "$missing" >&2; exit 1; }

(cd "$XRIZER" && cargo xbuild --release)
ls "$XRIZER"/target/release/bin/linux64/vrclient.so
echo "BUILD OK: $(git -C "$SRC" rev-parse --short HEAD) + patches, xrizer $(git -C "$XRIZER" rev-parse --short HEAD)"
