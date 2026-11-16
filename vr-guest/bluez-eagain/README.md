# bluez-eagain: patched bluetoothd for the PS VR2 Sense controllers

solar's stock BlueZ (Bazzite ships terra-bluez 5.87) drops both Sense controllers about
30 s after SteamVR starts: an output report write returns `EAGAIN`, and bluetoothd
destroys the UHID device instead of retrying (bluez/bluez#2431, stale-closed and unfixed).
This directory rebuilds terra's exact bluetoothd with two extra patches and runs it from
`/usr/local` through a systemd drop-in. The image's `/usr/libexec/bluetooth/bluetoothd`
is never touched, so an `rpm-ostree` update cannot half-replace it.

| Patch | Source | What |
|---|---|---|
| `0001`–`0021` | terra-bluez `5.87+1.git8750129efca8-2` spec | Terra's own patch set (Valve/SteamOS), so the build matches the image |
| `0100-input-device-drop-output-report-on-EAGAIN` | local (homelab-ops#389) | Drop interrupt-channel output reports on `EAGAIN` for any device; logs `BT interrupt socket busy: dropped N output report(s)` |
| `0101-input-Do-not-force-active-mode-by-default` | RealSupremium/bluez `fadd645` | Sniff-mode patch PSVR2Toolkit exp-3 needs for its 133 Hz controller poll. Turn it off without a rebuild: `DisableForceActive=false` under `[General]` in `/etc/bluetooth/input.conf` |

## Build and install (on solar)

```bash
./build-in-podman.sh      # writes ./bluetoothd and build/configure/make logs
sudo ./install.sh         # /usr/local/libexec/bluetooth/bluetoothd + bluetooth.service.d/50-eagain-patch.conf
sudo ./rollback.sh        # back to the image's stock bluetoothd
```

## After a Bazzite BlueZ update

The drop-in keeps running the old patched binary. `install.sh` records the terra-bluez it
was installed against in `bluetoothd.built-for`; `solar-check` fails
`bluez_build_matches` once the image's terra-bluez differs, which alerts as
`SolarPatchCheckFailed`. Either roll back, or rebuild against
the new source: take the new terra-bluez spec, replace patches `0001`–`0099` with its
patch set, point the `curl` line in `build.sh` at its git commit, and check that
`0100` and `0101` still apply (`patch -F3` fails loudly when they don't).
