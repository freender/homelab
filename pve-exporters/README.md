# homelab/pve-exporters

Prometheus-native host metrics exporters for Proxmox and ZFS storage hosts.

## Hosts
- ace (Proxmox)
- bray (Proxmox)
- clovis (Proxmox)
- osiris (Proxmox)
- cinci (Ubuntu offsite, `runtime: docker`)
- cottonwood (Ubuntu offsite, `runtime: docker`)

### Offsite Ubuntu hosts (`runtime: docker`)

Offsite hosts `cinci` and `cottonwood` set `pve-exporters.runtime: docker` in
`hosts.conf`. In this mode the module does **not** install native
`prometheus-node-exporter`/`smartctl-exporter` packages and does **not** own the
Docker exporter stack: `node-exporter`, `smartctl-exporter`, and `cadvisor` remain
host-managed under `/mnt/cache/appdata/<host>-exporters/compose.yml` and are scraped
by `vmagent` on `helm` (targets in `vmagent/scrape.yml`).

What the module **does** manage on these hosts:
- The host-native `zfs-pool-textfile-exporter` script + systemd service/timer that
  writes `homelab_zpool_*` metrics to `/var/lib/prometheus/node-exporter/zfs-pools.prom`.

It does not modify the `node-exporter`/`smartctl-exporter`/`cadvisor` compose services,
networks, or any other host-specific compose tuning. The compose file itself is entirely
host-managed — the module never reads or writes it.

#### Required manual compose settings (host compose, not repo-managed)

Because the module writes the ZFS textfile metrics to
`/var/lib/prometheus/node-exporter/` but does not touch compose, the `node-exporter`
service in both host compose files must be configured **manually** with the settings
below for those metrics — and the systemd collector — to actually reach Prometheus. They
are load-bearing for the fleet-wide `SystemdUnitFailed` vmalert rule on `helm` and for
`homelab_zpool_*` visibility:

```yaml
    security_opt:
      - apparmor=unconfined
    command:
      - '--collector.systemd'
      - '--collector.textfile'
      - '--collector.textfile.directory=/host/var/lib/prometheus/node-exporter'
    volumes:
      - /run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket
      - /var/lib/prometheus/node-exporter:/host/var/lib/prometheus/node-exporter:ro
```

- The mount destination must be `/var/run/...`, not `/run/...`: go-systemd dials the
  `/var/run` path and the image is scratch-based with no `/var/run` -> `/run` symlink.
- `apparmor=unconfined` is required because Ubuntu's `docker-default` profile denies the
  D-Bus `Hello` method call (`An AppArmor policy prevents this sender from sending this
  message to this recipient`).
- The container still runs as the image's unprivileged `nobody` user. Reading unit state
  over the system bus is allowed for unprivileged callers; polkit denies unit start/stop,
  so this is read-only access in practice.
- If any of these settings is missing or dropped in a future compose edit,
  `homelab_zpool_*` and/or `node_systemd_units` silently disappear from Prometheus while
  the container itself stays `up`. The `SystemdCollectorFailing` rule on `helm` exists to
  catch the systemd-collector case (`node_scrape_collector_success{collector="systemd"}
  == 0`); there is no equivalent alert yet for the textfile collector going missing.

This replaced `systemd-failed-textfile-exporter`, a host-native textfile fallback that
emitted `homelab_systemd_units_failed_total` purely because the containerized exporter had
no dbus access. `scripts/install.sh` now removes that fallback wherever it was installed. Deploy is over the offsite root SSH path (`config.user: root`,
`sshkey: offsite`), which requires the offsite key loaded in the shared agent
(`addoffsitekey` on `riven`).

## What It Collects
- Host metrics via node_exporter (CPU, memory, load, uptime, disk, network, hwmon, ZFS)
- ZFS pool capacity metrics via node_exporter textfile collector (`homelab_zpool_*`)
- SMART metrics via smartctl_exporter
- UPS metrics via apcupsd exporter on `master` / `master-standalone` UPS hosts
- Intel GPU metrics via `igpu-exporter` on selected PVE hosts

## Ports
- node_exporter: `:9100`
- smartctl_exporter: `:9633`
- apcupsd exporter: `:9162`
- igpu-exporter: `:9400` on `ace`, `bray`, and `clovis`

## Configuration Files

**On target hosts:**
- `/etc/default/prometheus-node-exporter`
- `/usr/local/bin/zfs-pool-textfile-exporter`
- `/etc/systemd/system/zfs-pool-textfile-exporter.service`
- `/etc/systemd/system/zfs-pool-textfile-exporter.timer`
- `/etc/systemd/system/smartctl-exporter.service`
- `/etc/default/smartctl-exporter`
- `/usr/local/bin/apcupsd-exporter`
- `/etc/systemd/system/apcupsd-exporter.service`
- `/etc/default/apcupsd-exporter`
- `/usr/local/bin/igpu-exporter`
- `/etc/systemd/system/igpu-exporter.service`
- `/etc/default/igpu-exporter`

**In this repo:**
- `configs/common/node-exporter.defaults`
- `configs/common/zfs-pool-textfile-exporter`
- `configs/common/zfs-pool-textfile-exporter.service`
- `configs/common/zfs-pool-textfile-exporter.timer`
- `configs/common/smartctl-exporter-override.conf`
- `configs/common/apcupsd-exporter.py`
- `configs/common/apcupsd-exporter.service`
- `configs/common/apcupsd-exporter.env`
- `configs/common/igpu-exporter.py`
- `configs/common/igpu-exporter.service`
- `configs/common/igpu-exporter.defaults`
- `../deploy`

### Where each exporter binary comes from

`apt` owns every binary it can. Only `igpu-exporter` is built by hand, and only
because there is no packaged or released artifact to use.

| Exporter | Source | Why |
|---|---|---|
| `prometheus-node-exporter` | `apt`, Debian `main` | packaged |
| `smartctl_exporter` | `apt`, `<codename>-backports` (`prometheus-smartctl-exporter`) | packaged, but Debian stable ships it only in backports |
| `igpu-exporter` | this repo (`configs/common/igpu-exporter.py`) | not packaged anywhere and upstream ships **zero** releases; a ~40-line wrapper over `intel_gpu_top` beats compiling a Go project on every host |
| `apcupsd-exporter` | this repo (`configs/common/apcupsd-exporter.py`) | homelab-specific script |

Nothing in this module downloads anything at deploy time any more, so `curl`,
`tar` and the `golang-go` toolchain are no longer installed. `python3` (runs the
two in-repo exporters) and `intel-gpu-tools` are installed via `apt` as needed;
`smartmontools` arrives as a dependency of `prometheus-smartctl-exporter`.

#### igpu-exporter

Previously the third-party Go exporter (`mike1808/igpu-exporter`), compiled from
a pinned git revision on every host because upstream publishes no release
artifacts and it is packaged nowhere. That meant installing the whole
`golang-go` toolchain (~250 MB) on `ace`/`bray`/`clovis` and doing a
build-from-source at deploy time, to produce a binary that just shelled out to
`intel_gpu_top` anyway.

`configs/common/igpu-exporter.py` reads `intel_gpu_top -J` directly. Metric
names, HELP strings, TYPEs and `engine` label values are byte-identical to the
Go exporter's output (verified by diffing `/metrics` before and after), so the
`intel-gpu` scrape job in `vmagent/scrape.yml` and the
`igpu_engines_busy_percent` panels in Grafana needed no changes. The one
addition is `igpu_up`, which is 0 when no current `intel_gpu_top` sample is
available.

Implementation notes worth knowing before editing it:

- `intel_gpu_top -J` **streams** samples (a `[` then concatenated objects with no
  separating commas), so a reader thread keeps the newest sample and `/metrics`
  serves whatever is current.
- Sampling once per scrape would not work: the first sample is a ~0 ms warm-up
  with every counter zeroed, so a one-shot invocation reports a permanently idle
  GPU regardless of real load.
- The reader supervises its own `intel_gpu_top` child and respawns it after 5s,
  so a transient failure does not become a systemd restart loop. While no
  sample is available it publishes `igpu_up 0` and **omits** the gauges rather
  than serving the last known values, so a stalled exporter cannot masquerade as
  an idle GPU.

If `go` is not used for anything else on these hosts, the now-unused toolchain
can be reclaimed manually (~250 MB): `apt purge golang-go && apt autoremove`.
The `golang-github-containers-*` packages are unrelated Proxmox dependencies and
must stay.

#### smartctl_exporter and Debian backports

`install.sh` writes `/etc/apt/sources.list.d/debian-backports.sources` (suite
derived from `/etc/os-release` at install time, so it survives a Debian major
upgrade) and installs `prometheus-smartctl-exporter` with
`-t <codename>-backports`. The backport is the same upstream version this module
previously downloaded by hand, so nothing regresses by letting `apt` own it, and
in exchange:

- `apt` handles upgrades, so there is no hand-rolled download + version-detection
  logic to get wrong (that check was silently broken and reinstalled the binary
  on every deploy).
- The package is signed and integrity-checked; the old path fetched a tarball
  over HTTPS and ran it as root with no checksum verification.
- `smartmontools` is a declared dependency instead of a separate `apt` call.

Enabling backports is safe next to the Proxmox repos: Debian backports sets
`NotAutomatic: yes` with `ButAutomaticUpgrades: yes`, so nothing is ever pulled
from it implicitly, but this package does stay current once installed.

The packaged unit is `smartctl_exporter.service` (underscore) and its `ExecStart`
takes no arguments, so the module installs a drop-in at
`/etc/systemd/system/smartctl_exporter.service.d/override.conf` to set the flags
it needs — notably `--smartctl.interval=10s` (matching the `pve-smartctl`
`scrape_interval` in `vmagent/scrape.yml` on `helm`) and
`--smartctl.powermode-check=standby`, which keeps the exporter from waking disks
that the `disk-spindown` module has parked. Deploys migrate off the old
self-managed `smartctl-exporter.service` (hyphen) automatically, tearing it down
before the package installs so the two never fight over `:9633`.

## Deployment

Deploy to all supported hosts:
```bash
cd ~/homelab
./deploy pve-exporters all
```

Deploy to specific hosts:
```bash
./deploy pve-exporters ace
./deploy pve-exporters bray
./deploy pve-exporters cinci        # runtime: docker; offsite key must be loaded
./deploy pve-exporters cottonwood   # runtime: docker; offsite key must be loaded
```

## Verification

```bash
ssh bray "systemctl status prometheus-node-exporter"
ssh bray "systemctl status smartctl-exporter"
ssh bray "curl -s http://127.0.0.1:9100/metrics | head"
ssh bray "curl -s http://127.0.0.1:9633/metrics | head"
```

## Removal

```bash
./remove.sh all
./remove.sh --purge all
```

**What it does:**
- Stops and disables smartctl-exporter service
- Stops and disables apcupsd-exporter service
- Removes smartctl-exporter binary and config
- Removes apcupsd-exporter binary and config
- Optionally purges node_exporter package
