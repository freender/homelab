# bluez-eagain: patched bluetoothd for the PS VR2 Sense controllers

solar's stock BlueZ (Bazzite ships terra-bluez 5.87) drops both Sense controllers about
30 s after SteamVR starts. An output report write returns `EAGAIN`, and bluetoothd
destroys the UHID device instead of retrying (bluez/bluez#2431, closed as stale and never
fixed). This directory rebuilds terra's exact bluetoothd with two extra patches and runs
it from `/usr/local` through a systemd drop-in. The image's `/usr/libexec/bluetooth/bluetoothd`
is never touched, so an `rpm-ostree` update cannot half-replace it.

| Patch | Source | What |
|---|---|---|
| `0001`–`0021` | terra-bluez `5.87+1.git8750129efca8-2` spec | Terra's own patch set (Valve/SteamOS), so the build matches the image |
| `0100-input-device-drop-output-report-on-EAGAIN` | local (homelab-ops#389) | Drop interrupt-channel output reports on `EAGAIN` for any device; logs `BT interrupt socket busy: dropped N output report(s)` |
| `0101-input-Do-not-force-active-mode-by-default` | RealSupremium/bluez `fadd645` | Sniff-mode patch PSVR2Toolkit exp-3 needs for its 133 Hz controller poll. Turn it off without a rebuild: `DisableForceActive=false` under `[General]` in `/etc/bluetooth/input.conf` |

## Build and install: the `vr-guest` bluez stage

`./deploy vr-guest solar` handles both (homelab-ops#405).

**Build.**
- The stage fingerprints `build.sh`, `patches/`, `BLUEZ_TERRA` and `BLUEZ_IMAGE` (both in
  `src/homelab/modules/vr_guest.py`).
- It rebuilds only when that fingerprint differs from the one recorded in
  `/usr/local/libexec/bluetooth/bluetoothd.inputs`.
- The build runs `build.sh` in a throwaway root Podman container from `BLUEZ_IMAGE` and
  takes minutes.
- The work dir and the build/configure/make logs stay in `/var/cache/vr-guest/bluez-eagain/`.

**Install.**
- It installs the binary with the `bluetooth_exec_t` SELinux context.
- It writes `bluetoothd.built-for` (the `rpm -q terra-bluez` it was built for) and the
  inputs stamp.
- It installs the drop-in `50-eagain-patch.conf` into `/etc/systemd/system/bluetooth.service.d/`.
- It restarts `bluetooth`, but only when something changed.

**Guards.**
- **SteamVR running:** the stage does nothing while SteamVR runs. The build is minutes of
  CPU, and the restart drops the controllers. A build finished just as SteamVR started is
  kept and installed by the next deploy.
- **BlueZ version mismatch:** the stage refuses to build when the booted image's
  terra-bluez is not `BLUEZ_TERRA`. A patch set written for another version is never
  built.

`solar-check` covers it with `patched_bluetoothd`, `bluez_build_matches`,
`bluez_build_inputs` and `bluez_dropin`. When the stage ran, the deploy fails on any of
them.

## After a Bazzite BlueZ update

`SolarUpdateStaged` lists a bluez change the day the image is staged. After the
power-cycle, the drop-in keeps running the old patched binary. `bluez_build_matches` then
fails, which alerts as `SolarPatchCheckFailed`, and the deploy refuses until the inputs
match. Either roll back, or move the inputs to the new source:

1. Take the new terra-bluez spec and replace patches `0001`–`0099` with its patch set.
2. Set `BLUEZ_TERRA` to the new `rpm -q terra-bluez`. `build.sh` fetches the bluez commit
   named in it (`.git<hash>-`), so nothing else pins the source. If the spec's `./configure`
   flags changed, update them in `build.sh`.
3. Run `./deploy vr-guest solar` with SteamVR quit. If `0100` or `0101` no longer apply,
   the build fails loudly (`patch -F3`), and its logs are in the work dir.

## Rollback

`sudo ./rollback.sh` on solar returns `bluetooth.service` to the image's stock bluetoothd.
It is an emergency switch, not a setting: the next `./deploy vr-guest solar` installs the
patch again.
