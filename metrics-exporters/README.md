# homelab/metrics-exporters

Prometheus-native host metrics exporters for Proxmox, Ubuntu, and LXC guest hosts.

## Hosts

Bare metal (`node-exporter` + `smartctl_exporter` + ZFS textfile exporter):
- ace, bray, clovis, osiris (Proxmox, Debian)
- cinci, cottonwood (Ubuntu, offsite)

LXC guests, `metrics-exporters.lxc_guest: true` (`node-exporter` only):
- helm (on clovis), neo (on bray), tower (on ace)

Every host runs host-native exporters; there is no per-host runtime mode. Only
`cadvisor` remains containerised, in the host-managed compose stack under
`/mnt/cache/appdata/.../compose.yml`, which this module never reads or writes.
cadvisor stays a container deliberately: it is not packaged for Debian/Ubuntu, it
needs the docker socket and cgroups, and monitoring containers is its whole job.

### LXC guests (`lxc_guest: true`)

A guest shares the PVE host's kernel, so most "hardware" it can see is not its
own. It therefore gets a reduced collector set and **no** `smartctl_exporter`
(there are no disk device nodes to probe) and **no** ZFS textfile exporter
(`/dev/zfs` is absent, so `zpool` cannot run -- deploying it would just leave a
failed unit and fire `SystemdUnitFailed`).

Crucially, node_exporter keeps its **default** collectors enabled regardless of
which `--collector.*` flags are listed, so the host-hardware ones have to be
negated explicitly. Otherwise the guest republishes the PVE host's data under its
own `host` label, double-counting anything aggregated across hosts
(`node_zfs_arc_size` alone is referenced ~193 times in Grafana). Negated for
guests, each verified against a live guest:

| Collector | Why it is the host's, not the guest's |
|---|---|
| `zfs` | `/proc/spl/kstat/zfs` is the host's ARC |
| `hwmon` | `/sys/class/hwmon` is the host's sensors |
| `diskstats` | lxcfs passes the host's disks through -- neo showed *bray's* NVMes, plus partitions/loops that bray does not even report itself |
| `nvme` | `/sys/class/nvme` is the host's controllers |
| `thermal_zone`, `edac` | host thermal zones and ECC counters |

Kept, because lxcfs virtualises them to the guest's own limits or they are
genuinely namespaced: `cpu`, `meminfo`, `loadavg`, `filesystem`, `netdev`,
`systemd`, `textfile`, `uname`.

Going native on the guests also fixed a long-standing bug: the containerised
exporter reported `/proc/net/dev` from its **own** network namespace, so
`node_network_*` measured the exporter container's traffic. helm was reporting
`eth0`/`eth1` while its real interface is `nic0`.

### Previously: `runtime: docker` on the offsite Ubuntu hosts

`cinci` and `cottonwood` used to run node-exporter and smartctl-exporter as
containers, with the module installing only `zfs-pool-textfile-exporter`
natively. That is retired, because running them natively removed several
fragile, invisible dependencies:

- **The dbus + AppArmor workaround is gone.** The containerised node-exporter
  needed `security_opt: apparmor=unconfined` plus a bind-mount of
  `/run/dbus/system_bus_socket` to `/var/run/dbus/system_bus_socket` purely so
  `--collector.systemd` would work, because Ubuntu's `docker-default` profile
  denies the D-Bus `Hello` call. Natively there is nothing to work around.
- **The textfile bind-mount requirement is gone.** `homelab_zpool_*` reached
  Prometheus only because compose mounted
  `/var/lib/prometheus/node-exporter` into the container and set
  `--collector.textfile.directory` to the mounted path. Native node-exporter
  reads that directory directly.
- **Both of those were manual, undeclared compose settings.** Dropping any of
  them in a future compose edit silently lost `node_systemd_units` or
  `homelab_zpool_*` while the container stayed `up` — the failure mode
  `SystemdCollectorFailing` exists to catch.
- **Host network metrics were simply wrong.** `/proc/net/dev` is
  network-namespace-scoped, so the container's netdev collector reported its own
  `eth0`/`lo` rather than the host's interfaces. Natively these hosts now report
  real uplink traffic on `nic0`.

`systemd-failed-textfile-exporter`, a host-native fallback that existed only
because the container had no dbus access, was retired earlier;
`scripts/install.sh` still removes it wherever it was installed.

Deploy to these hosts is over the offsite root SSH path (`config.user: root`,
`sshkey: offsite`), which requires the offsite key loaded in the shared agent
(`addoffsitekey` on `riven`).

### Migrating a host off containerised exporters

`install.sh` refuses to run while a container named `node-exporter` or
`smartctl-exporter` is up, because the native units cannot bind `:9100`/`:9633`
underneath it and the package postinst would fail half-way. The compose file is
host-managed, so removing those two services is a manual step:

```bash
cd /mnt/cache/appdata/<host>-exporters
cp -a compose.yml compose.yml.bak-pre-native-$(date +%Y%m%d%H%M%S)
# delete the node-exporter and smartctl-exporter service blocks; keep cadvisor
docker compose config --services      # expect: cadvisor
docker compose up -d --remove-orphans
```

Then `./deploy metrics-exporters <host>` from the repo.

## What It Collects
- Host metrics via node_exporter (CPU, memory, load, uptime, disk, network, hwmon, ZFS)
- ZFS pool capacity metrics via node_exporter textfile collector (`homelab_zpool_*`)
- SMART metrics via smartctl_exporter
- UPS metrics via apcupsd exporter on `master` / `master-standalone` UPS hosts
- Intel GPU metrics via `igpu-exporter` on selected PVE hosts
- SAS HBA controller temperature via node_exporter textfile collector
  (`homelab_hba_*`) on hosts with `hba_temp: true`

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
- `/usr/local/bin/hba-temp-textfile-exporter`
- `/etc/systemd/system/hba-temp-textfile-exporter.service`
- `/etc/systemd/system/hba-temp-textfile-exporter.timer`

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
- `configs/common/hba-temp-textfile-exporter.py`
- `configs/common/hba-temp-textfile-exporter.service`
- `configs/common/hba-temp-textfile-exporter.timer`
- `../deploy`

### Where each exporter binary comes from

`apt` owns every binary it can. Only `igpu-exporter` is built by hand, and only
because there is no packaged or released artifact to use.

| Exporter | Source | Why |
|---|---|---|
| `prometheus-node-exporter` | `apt`, Debian `main` | packaged |
| `smartctl_exporter` | `apt` (`prometheus-smartctl-exporter`) | packaged; Ubuntu has it in the normal archive, Debian stable only in `<codename>-backports` |
| `igpu-exporter` | this repo (`configs/common/igpu-exporter.py`) | not packaged anywhere and upstream ships **zero** releases; a ~40-line wrapper over `intel_gpu_top` beats compiling a Go project on every host |
| `apcupsd-exporter` | this repo (`configs/common/apcupsd-exporter.py`) | homelab-specific script |
| `hba-temp-textfile-exporter` | this repo (`configs/common/hba-temp-textfile-exporter.py`) | no exporter and no vendor tool reads a SAS2 HBA's temperature on Linux; see below |

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

On Debian, `install.sh` writes
`/etc/apt/sources.list.d/debian-backports.sources` (suite derived from
`/etc/os-release` at install time, so it survives a Debian major upgrade) and
installs `prometheus-smartctl-exporter` with `-t <codename>-backports`. On Ubuntu
the package is in the normal archive, so no extra repo is added — and a
backports file left by an earlier Debian-shaped deploy is removed. The backport is the same upstream version this module
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
that the `disk-spindown` module has parked. `--smartctl.path` is per-host: hosts
setting `metrics-exporters.smartctl_wrapper: true` get
`/usr/local/bin/homelab-smartctl-wrapper` instead of `smartctl` itself.

##### smartctl_wrapper (cottonwood)

`cottonwood` sets `metrics-exporters.smartctl_wrapper: true`, which deploys
`configs/common/smartctl-wrapper.sh`. Its USB-attached NVMe disks sit behind
ASMedia bridges that (a) `smartctl --scan` does not report at all, so the
exporter would never probe them, and (b) return `exit_status 4` with "Read 1
entries from Error Information Log failed" even when the SMART data is fine,
which the exporter treats as a dead device. The wrapper injects the two devices
into the scan output as `type: sntasmedia` and downgrades that one benign error.
Without it this host reports 2 SMART devices instead of 4. Deploys migrate off the old
self-managed `smartctl-exporter.service` (hyphen) automatically, tearing it down
before the package installs so the two never fight over `:9633`.

#### hba-temp-textfile-exporter (`hba_temp: true`)

`ace` and `clovis` each carry an LSI SAS9207-8i (SAS2308, IT-mode firmware
P20). Every other hot component on those hosts reports a temperature —
CPU package and cores via `coretemp`, NVMe via its own hwmon, the X540 NIC via
`ixgbe` — but the HBA reported nothing, because `mpt3sas` registers no hwmon
device. That is the one card with no fan of its own and no airflow guarantee,
and it was invisible. Measured on first read: **ace 95 °C, clovis 115 °C** IOC
die temperature, against a 115 °C maximum for the ASIC.

Nothing off the shelf reads it on Linux:

| Option | Result |
|---|---|
| `sensors` / hwmon | no HBA chip; `mpt3sas` registers none |
| `smartctl_exporter` | reports the *disks* behind the HBA, not the controller |
| `storcli` | MegaRAID and SAS3 HBAs only; returns "No Controller found" for a SAS2308 |
| `sas2ircu` | Broadcom's SAS2 tool, but it has no temperature command |
| FreeBSD `mpsutil` / TrueNAS CORE `sysctl` | works there, does not exist on Linux |

The value does exist in firmware: MPI2 **IO Unit Page 7** carries
`IOCTemperature` plus its unit code. `mpt3sas` exposes a message-passing
character device (`/dev/mpt2ctl` for SAS2 hardware, `/dev/mpt3ctl` for SAS3)
that passes a config-page request through to firmware — the same interface
`storcli` uses on the cards it does support. `configs/common/hba-temp-textfile-exporter.py`
issues that two-step read (`PAGE_HEADER` for the page length, then
`READ_CURRENT`) and writes
`/var/lib/prometheus/node-exporter/hba-temp.prom`.

Both requests are read-only, and the kernel driver owns the DMA buffer and
builds the scatter-gather element itself — the script only fills in the first
28 bytes of the message frame and never touches hardware registers.

Controllers come from `/sys/class/scsi_host/*` rather than blind IOC probing:
`proc_name` identifies the generation (`mpt2sas` vs `mpt3sas`, which selects the
device node), `unique_id` *is* the IOC number the ioctl wants, and `board_name` /
`version_product` / `version_fw` supply the labels. The PCI address is the
identity label because scsi host numbering is not stable across reboots.

Metrics (no `host` label — the scrape config attaches one, and emitting our own
would only add a redundant `exported_host`, as `homelab_zpool_*` does):

```
homelab_hba_temperature_celsius{pci,board,chip,sensor="ioc"}
homelab_hba_temperature_read_success{pci,board,chip}
homelab_hba_info{pci,board,chip,driver,firmware}
```

`sensor` exists because IO Unit Page 7 also defines a board sensor; the 9207-8i
reports `NOT_PRESENT` for it, so only `sensor="ioc"` is emitted here. A card
whose firmware omits the sensor entirely (some OEM rebadges) sets
`read_success 0` rather than vanishing, so the failure is visible instead of
being an absent series.

The timer runs every minute. The firmware samples the die on its own slow
interval, so polling faster adds firmware round-trips without adding resolution.
Finding no controller at all is a hard failure (exit 1 → failed unit →
`SystemdUnitFailed`): the exporter is only deployed where a card is declared, so
its absence means the card or driver is gone.

## Deployment

Deploy to all supported hosts:
```bash
cd ~/homelab
./deploy metrics-exporters all
```

Deploy to specific hosts:
```bash
./deploy metrics-exporters ace
./deploy metrics-exporters bray
./deploy metrics-exporters cinci        # offsite key must be loaded
./deploy metrics-exporters cottonwood   # offsite key must be loaded
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
