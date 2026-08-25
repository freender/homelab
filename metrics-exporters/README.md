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
- SAS HBA controller temperature and per-PHY link health via node_exporter
  textfile collector (`homelab_hba_*`) on hosts with `hba: true`
- Pending-reboot state via node_exporter textfile collector
  (`homelab_reboot_required`, `homelab_kernel_info`) on bare metal
- Human-readable disk names via node_exporter textfile collector
  (`homelab_disk_label`, `homelab_smart_disk_label`) on bare metal

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
- `/usr/local/bin/hba-textfile-exporter`
- `/etc/systemd/system/hba-textfile-exporter.service`
- `/etc/systemd/system/hba-textfile-exporter.timer`
- `/usr/local/bin/reboot-textfile-exporter`
- `/etc/systemd/system/reboot-textfile-exporter.service`
- `/etc/systemd/system/reboot-textfile-exporter.timer`
- `/usr/local/bin/disk-label-textfile-exporter`
- `/etc/systemd/system/disk-label-textfile-exporter.service`
- `/etc/systemd/system/disk-label-textfile-exporter.timer`
- `/etc/homelab/disk-labels.conf`

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
- `configs/common/hba-textfile-exporter.py`
- `configs/common/hba-textfile-exporter.service`
- `configs/common/hba-textfile-exporter.timer`
- `configs/common/reboot-textfile-exporter`
- `configs/common/reboot-textfile-exporter.service`
- `configs/common/reboot-textfile-exporter.timer`
- `configs/common/disk-label-textfile-exporter.py`
- `configs/common/disk-label-textfile-exporter.service`
- `configs/common/disk-label-textfile-exporter.timer`
- `templates/disk-labels.conf.tpl`
- `../deploy`

### Pending-reboot reporting

`apt-upgrade` installs kernel updates on a daily timer and never reboots; its
only signal was a `print_warn` during a deploy run. `reboot-textfile-exporter`
turns that into `homelab_reboot_required` (0/1) plus `homelab_kernel_info`,
which carries the running and newest-installed kernel release as labels.
`vmalert-rules/configs/reboot.yml` alerts on it after 24h.

Bare metal only, via the same `baremetal` gate as the ZFS exporter. An LXC guest
runs the PVE host's kernel, reports it in `uname -r`, and has no kernel packages
of its own, so it has nothing to reboot into. `ghost` is flagged `lxc_guest` and
is excluded by the same gate, which is also correct for a different reason — WSL
takes its kernel from Windows, not from apt.

It compares the running kernel against the newest **installed** kernel package
rather than reading `/var/run/reboot-required`. That file comes from an apt hook
in `update-notifier-common`, which is a Ubuntu default and is present on `cinci`
only; on the Proxmox nodes and `cottonwood` it is never created, so a check built
on it alone would report "no reboot needed" forever. The file is still read as a
supplementary signal, since where it does exist it also catches non-kernel
reboots that a kernel comparison cannot see.

**Do not alert on `node_reboot_required`.** That series already exists on every
host, emitted into `apt.prom` by `apt_info.py` from the distro package
`prometheus-node-exporter-collectors`, and it is precisely the
`/run/reboot-required` check described above. Verified 2026-08-16 on `ace`,
`bray`, `clovis` and `osiris`: all four report `node_reboot_required 0.0` with
that package installed and `update-notifier-common` absent, so on Debian/Proxmox
the value is structurally 0 and can never become 1. It is trustworthy only on
the Ubuntu hosts. `homelab_reboot_required` exists under its own name for this
reason, and this module deliberately does not overwrite `apt.prom`.

Kernel releases are derived from package names, so no Proxmox or Ubuntu series
is hardcoded and a major upgrade does not break the check. Only `ii` packages
count: `un` rows have blank versions (on PVE the unsigned name is virtual,
provided by the real `-signed` package) and `rc` rows carry real but stale
versions that would otherwise fabricate a pending reboot. Meta and helper
packages (`linux-image-generic`, `proxmox-kernel-7.0`, `proxmox-kernel-helper`)
encode no release and are skipped — without that, `proxmox-kernel-helper 9.2.0`
sorts above every real kernel. `tests/test_reboot_exporter.py` covers each of
these cases against live-host output.

### disk-label-textfile-exporter

Grafana graphs one series per physical disk, and a raw serial is unreadable in a
legend. That used to be fixed in the dashboard: **966 hand-written field
overrides** mapping `serial -> "Bray VM-Flash (2TB)"`, duplicated across ten disk
panels, so every disk swap meant editing ten panels by hand.

It had already rotted by the time it was replaced (2026-08-20). Three serials
belonged to disks that no longer existed, and the same serial carried different
names on different panels -- harmless only because each panel filters
`rotational=`, so the wrong half never matched. Each panel carried the full map
regardless of which half it could use.

This exporter deletes the map rather than relocating it. A name is
`<pool> [<position>]`, all of which is readable from the running system, so
nothing is written down by a human and **no hardware identifier enters the
repo**.

| Component | Derived from |
|---|---|
| `rpool`, `vm-flash`, `vault-hdd`, `cache` | the pool the disk belongs to, verbatim |
| `boot` | the disk backing `/`, when `/` is not on ZFS (cinci, cottonwood) |
| `D1..D6` | the member's ordinal in that pool, in the order `zpool status` prints it -- which is vdev order, not device order (clovis's vault starts at `sde`). Omitted for a single-member pool. |

Panels group by `(host, disk_label)` and render `{{host}} {{disk_label}}`, so a
disk reads as `clovis vault-hdd D1`.

#### What the name deliberately leaves out

**Capacity.** It disambiguates nothing -- clovis has five identical 10 TB
members -- and printing it needs a rounding heuristic, because disks are sold in
decimal units that never land exactly on the number on the box. Every tolerance
loose enough to turn 20,000,588,955,648 bytes into `20TB` also turns a 1.92 TB
enterprise SSD into `2TB`, since those are sized exactly 4% below round. The
exact byte count is published as the `size_bytes` label instead, where no
rounding decision is needed. node_exporter has no whole-disk size metric, so
this is the only source for it.

**Vdev geometry.** An earlier version named raidz members `Z1` and mirror
members `A`/`B`. That was dropped for two reasons. It makes identity depend on
layout, so rebuilding vault-hdd as raidz2 would rename all nine of its disks and
orphan their series -- pool names change far less often than pool layouts. And
it was the sole reason this exporter needed an indentation-aware parse of the
`zpool status` tree: container-vdev detection, section headers, per-vdev counter
resets, letters-versus-numbers. Keyed on the pool, position is just the member's
ordinal and the parser is a flat scan for lines beginning with `/`.

Two consequences of the flat scan, both acceptable and neither reachable in this
fleet: a pool with several vdevs numbers straight through them rather than
restarting per vdev, and a cache/log/spare device would be numbered inline with
the data members.

**The host, and any prettifying of the pool name.** Every other panel in the
dashboard builds its legend as `{{host}} <thing>` from the `host` label the
scrape config attaches. A name that carried its own Title-cased host was
therefore the one series in the dashboard that could never match its neighbours:
`Ace Vault D1` sat next to `ace vm-flash` (from `homelab_zpool_*`) and
`ace - SAS9207-8i` (from `homelab_hba_*`), three spellings of one host. For the
same reason the pool name is emitted verbatim -- `vault-hdd`, not `Vault`. It is
what `zpool status` prints and what `homelab_zpool_*` already labels it, so a
cosmetic map only created a fourth spelling of one pool. Dropping both also
deleted the map, the host-casing helper, and the fleet-uniqueness constraint
described below.

#### No serial label

`serial` is deliberately absent. It is not the join key -- queries join on
`(host, device)` -- and it is genuinely ambiguous: for a USB-attached disk the
kernel and smartctl disagree. cottonwood `sdb` is `Y93814AW0JNFS6S` to
node_exporter and `S6SFNJ0WA41839Y` to smartctl, and cinci `sda` reports a bridge
placeholder of all zeroes. The old dashboard needed *two* override entries per
USB disk for exactly this reason. `node_disk_info` still carries the serial for
anyone who wants it, joinable on the same `(host, device)`.

#### Two metrics, because the exporters disagree on device names

```
homelab_disk_label{device="nvme0n1",...}        joins node_disk_* / node_disk_info
homelab_smart_disk_label{device="nvme0",...}    joins smartctl_device*
```

smartctl probes the NVMe controller, node_exporter reports the namespace block
device; SATA disks are named identically by both. Emitting both is what lets a
panel replace a nested `label_replace` chain with one `group_left`:

```promql
sum by(host,disk_label) (irate(node_disk_read_bytes_total{device=~"sd[a-z]+"}[25s])
  * on(host,device) group_left(disk_label) homelab_disk_label{rotational="1"})

max by(host,disk_label) (smartctl_device_temperature{temperature_type="current"}
  * on(host,device) group_left(disk_label) homelab_smart_disk_label{rotational="1"})
```

Legend format is `{{host}} {{disk_label}}` in both cases.

Neither carries a `host` label: the scrape config attaches one, and emitting our
own would only produce a redundant `exported_host` (as `homelab_zpool_*` does).
Names are therefore only unique *within* a host -- ace and clovis both call a
disk `vault-hdd D1` -- which is why panels must group by `(host, disk_label)`
rather than `disk_label` alone. That makes a cross-host merge structurally
impossible rather than something a test has to keep watching.

Two failure modes are guarded because both are silent and both cost the whole
host's metrics rather than one series. node_exporter rejects an *entire* textfile
on a duplicate metric, so two NVMe namespaces on one controller cannot both claim
the same smartctl device -- the second is dropped with a reason on stderr. And an
OFFLINE raidz member keeps the by-id path it was added with (`-L` cannot resolve
an absent device), so it yields no series but still consumes its position;
otherwise replacing a failed disk would renumber every healthy disk behind it and
orphan their history. That case is live on ace, whose vault `D3` failed on
2026-08-10 while `D1`/`D2` kept their identity.
`tests/test_disk_label_exporter.py` covers both, plus the deliberate cross-host
name collision and the real `zpool status` output from all four layouts.

#### The escape hatch: `metrics-exporters.disk_labels`

For the rare disk whose useful name is not derivable -- in no pool, not the boot
disk -- an optional per-host map renders to `/etc/homelab/disk-labels.conf`:

```yaml
metrics-exporters:
  disk_labels:
    My Passport: passport
```

Keyed by the model in `/sys/block/<dev>/device/model`, matched exactly or as a
prefix (longest wins) so a firmware revision suffix does not have to be pinned.
Values are lowercase, like the pool names they sit alongside in a legend.
**Keyed by model, never by serial** -- a model is not a hardware identifier, so
this stays safe in a public repo. Only cottonwood uses it, for a USB drive whose
enclosure reports a different model than the drive inside it.

### Where each exporter binary comes from

`apt` owns every binary it can. Only `igpu-exporter` is built by hand, and only
because there is no packaged or released artifact to use.

| Exporter | Source | Why |
|---|---|---|
| `prometheus-node-exporter` | `apt`, Debian `main` | packaged |
| `smartctl_exporter` | `apt` (`prometheus-smartctl-exporter`) | packaged; Ubuntu has it in the normal archive, Debian stable only in `<codename>-backports` |
| `igpu-exporter` | this repo (`configs/common/igpu-exporter.py`) | not packaged anywhere and upstream ships **zero** releases; a ~40-line wrapper over `intel_gpu_top` beats compiling a Go project on every host |
| `apcupsd-exporter` | this repo (`configs/common/apcupsd-exporter.py`) | homelab-specific script |
| `hba-textfile-exporter` | this repo (`configs/common/hba-textfile-exporter.py`) | no exporter and no vendor tool reads a SAS2 HBA's temperature on Linux; see below |
| `disk-label-textfile-exporter` | this repo (`configs/common/disk-label-textfile-exporter.py`) | homelab-specific naming derived from this fleet's pool layout |


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

##### Late-enumerating disks (`ExecStartPre` device wait)

The same drop-in adds an `ExecStartPre` running
`configs/common/smartctl-exporter-wait-devices` (installed as
`/usr/local/bin/homelab-smartctl-wait-devices`), plus `TimeoutStartSec=300` so
systemd cannot kill that wait.

`smartctl_exporter` registers its Prometheus metric descriptors once, from the
devices it finds at startup, but keeps rescanning for new devices every 10
minutes. A disk that appears *after* startup is collected with descriptors that
were never registered, and client_golang's registry rejects the entire gather:

```
collected metric smartctl_device_attribute ... with unregistered descriptor
```

`/metrics` then returns HTTP 500 for **every** device, not just the late one,
and keeps doing so until the process is restarted — the rescan that causes it
also re-confirms it every 10 minutes, so there is no self-recovery.

`ace` hit this after its 2026-08-23 rebuild: booted 19:02:53, exporter started
19:03:10 seeing only the two NVMes, LSI SAS2308 finished enumerating `sda`/`sdb`
at 19:04:14, and the 19:13 rescan poisoned the registry. `ace` was absent from
the `pve-smartctl` job for two days — including its NVMe SMART data, which had
nothing to do with the late SAS disks. HBA hosts (`ace`, `clovis`) are the
obvious exposure, but any slow bus qualifies, which is why the wait is on the
plain `baremetal` gate rather than on `hba`.

The script polls `smartctl --json --scan` and returns once the device *name*
list has been identical for 20s, giving up after 180s and starting the exporter
anyway (a partial metrics outage beats a failed unit). It waits for stability
rather than an expected device count on purpose — a count would have to live in
`hosts.conf` and would drift the first time a disk is added or pulled. It takes
`--smartctl.path` as its argument so a wrapper host scans the same way the
exporter does. Deploy-time restarts skip the wait entirely: it exits immediately
once the host is past 300s of uptime, since this is a boot-ordering race and a
restart on a long-running host has nothing to wait for.

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

#### hba-textfile-exporter (`hba: true`)

`ace` and `clovis` each carry an LSI SAS9207-8i (SAS2308, IT-mode firmware
P20). Every other hot component on those hosts reports a temperature —
CPU package and cores via `coretemp`, NVMe via its own hwmon, the X540 NIC via
`ixgbe` — but the HBA reported nothing, because `mpt3sas` registers no hwmon
device. That is the one card with no fan of its own and no airflow guarantee,
and it was invisible. Measured on first read: **ace 95 °C, clovis 115 °C** IOC
die temperature, against a 115 °C maximum for the ASIC — clovis confirmed by
hand the same day, its heatsink too hot to touch, and again when opening the
case dropped it 116 °C -> 98 °C within minutes while ace, untouched, held at
95 °C. The sensor tracks airflow in real time.

clovis runs hotter even though it has a slot fan and its GPU is idle
(vfio-bound), so this is not case airflow. It drives 6 SAS links to ace's 2
(the card's own `BoardPowerRequirement` says 14 W vs 10 W into the same size
heatsink), and the two cards are not the same hardware: ace's is a genuine LSI
(`board_assembly` `H3-25412-00K`, `board_tracer` `SV52978426`, SAS address in
LSI's `500605b` range) while clovis's reports no board assembly, no serial and
SAS address `0x56c92bf0...` — a clone or cross-flashed OEM card with
unprogrammed manufacturing NVDATA.

**Resolved 2026-08-20.** Both cards now have directed 40 mm fans and the
readings above are historical: clovis settled at 64-67 °C, and ace went
95 °C -> 65 °C within five minutes of its fan going in. The alert thresholds in
`vmalert-rules/configs/temperatures.yml` were lowered to match — 85 °C warning
and 100 °C critical, against the old 100/110 that were only ever that high
because 96 °C *was* the passive baseline. At a 65 °C baseline the point of the
alert is now "the fan has failed", not "this card runs hot".

##### Series identity: board+chip, not PCI address

Two hardware changes in one week broke the original `pci`-keyed labelling:
clovis's card moved to a different PCIe slot (`0000:04:00.0` ->
`0000:02:00.0`), and ace's card was replaced outright (`LSISAS2008` FW
09.00.00.00 -> `LSISAS2308` FW 20.00.02.00 on 2026-08-13). The first orphaned a
series and made one card look like two, the second legitimately produced a new
one.

So metric series are now keyed on **board+chip** plus the `host` label the
scrape config attaches, and every volatile identifier moved to
`homelab_hba_info`:

| Label | On series | On `homelab_hba_info` | Why |
|---|---|---|---|
| `board`, `chip` | yes | yes | Changes only when the card is genuinely replaced, which *should* start a new series |
| `pci` | only if forced | yes | Identifies the slot, not the card |
| `sas_address` | no | yes | Strongest real identity — NVDATA-resident, survives a slot move, verified stable across six boots on clovis |
| `serial` (`board_tracer`) | no | yes | Empty on cross-flashed/OEM clones — clovis reports none, so it cannot be the key |

`sas_address` was the obvious candidate for the key and was rejected: it is an
opaque hex string in alert text, and keying on it would still churn on any card
replacement. board+chip reads well in a notification and each host has exactly
one HBA.

"Exactly one" is not guaranteed, though, and two identical cards in one host
would render byte-identical label sets — node_exporter rejects the *entire*
textfile on a duplicate metric, so that host would lose all HBA metrics rather
than just the ambiguous one. `disambiguate()` handles it: when board+chip is not
unique it puts `pci` back for that host's controllers and logs why, accepting
slot-move churn as the lesser problem. `tests/test_hba_exporter.py` covers both
paths.

Empty labels are dropped by Prometheus/VictoriaMetrics, so clovis's
`homelab_hba_info` has no `serial` at all. That is correct — do not substitute a
placeholder, which would make an unprogrammed card look like it has a serial.

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
`storcli` uses on the cards it does support. `configs/common/hba-textfile-exporter.py`
issues that two-step read (`PAGE_HEADER` for the page length, then
`READ_CURRENT`) and writes
`/var/lib/prometheus/node-exporter/hba.prom`.

If the temperature ever looks implausible, the same page carries two fields
that can be checked against ground truth without trusting the sensor:
`PCIeWidth` / `PCIeSpeed` must match `lspci`'s `LnkSta` (they read x4 @ 8GT/s
on both hosts, as `lspci` reports), and `BoardPowerRequirement` tracks how many
SAS links the card is driving. They sit 8 bytes either side of
`IOCTemperature`, so if those two are right the struct offsets are right and
the temperature is genuinely what the firmware reports.

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
homelab_hba_phy_errors_total{pci,board,chip,phy,type}
homelab_hba_phy_link_rate_gbps{pci,board,chip,phy}
```

The PHY metrics need no ioctl — the SAS transport class publishes them under
`/sys/class/sas_phy`, matched to their controller by scsi host index
(`phy-0:3` belongs to `host0`). They are the early warning temperature is not:
temperature says the card is stressed, `invalid_dword` / `running_disparity` /
`loss_of_dword_sync` / `phy_reset_problem` say a link is actually degrading.
All four were zero on all 8 PHYs of both cards when this was added, so the
baseline is clean and any growth is meaningful — `sas-links.yml` alerts on
accumulation, not on the raw counter, since these never reset except with the
controller. Link rate is graphed but deliberately not alerted on: an unused PHY
reads 0 and a real SATA-II device would sit at 3.0 Gbit forever.

Temperature and PHY collection are independent — a failed ioctl still yields
PHY counters, because the two failure modes have nothing to do with each other.

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
