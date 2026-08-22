from __future__ import annotations

from pathlib import Path

from ..build import copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import FileSpec, normalize_bool, run_module_deploy, write_file_map
from ..output import print_sub
from ..ssh import HostConnection, diff_many

REMOTE_ROOT = "/tmp/homelab-metrics-exporters"

# Path smartctl_exporter is pointed at. Hosts whose disks need the scan/exit-code
# workaround (metrics-exporters.smartctl_wrapper) get the wrapper instead of smartctl
# itself; see README.
SMARTCTL_BIN = "/usr/sbin/smartctl"
SMARTCTL_WRAPPER_BIN = "/usr/local/bin/homelab-smartctl-wrapper"

TEXTFILE_DIR = "/var/lib/prometheus/node-exporter"

# node_exporter flags shared by every host.
_NODE_EXPORTER_COMMON_ARGS = (
    "--web.listen-address=:9100",
    "--collector.cpu",
    "--collector.meminfo",
    "--collector.loadavg",
    "--collector.filesystem",
    # Debian's prometheus-node-exporter package (1.9.0-1+b4, the whole fleet's
    # version) compiles in a patched default for this flag that adds `mnt|media`
    # to upstream's exclude list -- see `mount-points-exclude` in
    # `prometheus-node-exporter --help`. Every ZFS dataset we care about is
    # bind-mounted under /mnt/cache or /mnt/tank, so the packaged default
    # silently dropped all of it (only `/` and tmpfs `/tmp` kept reporting).
    # This bit us on 2026-07-30 when tower/helm/neo/cinci/cottonwood moved from
    # a Docker-run node_exporter (upstream default, no /mnt exclusion) to this
    # native package -- Grafana's "Dataset Growth - 31d" panel went flat that
    # day. Pin the exclude list back to upstream's pre-patch value so /mnt
    # datasets are reported again; still excludes the pseudo-filesystems and
    # container storage churn the patch was trying to hide.
    "--collector.filesystem.mount-points-exclude="
    "^/(dev|proc|run|sys|var/lib/docker/.+|var/lib/containers/storage/.+)($|/)",
    "--collector.netdev",
    "--collector.textfile",
    f"--collector.textfile.directory={TEXTFILE_DIR}",
    "--collector.systemd",
    "--collector.uname",
    "--no-collector.xfs",
    # node_exporter refuses to start if device-include and device-exclude are
    # both non-empty, and some builds ship a non-empty default for the exclude
    # (Debian 12's 1.5.0 uses `^lo$`), so passing only the include is enough to
    # make it panic with "device-exclude & device-include are mutually exclusive".
    # deepstone hit that on 2026-08-15; it has since been upgraded to trixie, so
    # the whole fleet is on 1.9.0, which defaults the exclude to empty. This line
    # is kept deliberately: it states the intent (we filter by include, so there
    # is no exclude) rather than depending on a build's default, which is what
    # makes it version-independent for any future host built from an older base
    # image. Verified started-clean on both 1.5.0 and 1.9.0. Keep it immediately
    # before the include.
    "--collector.netdev.device-exclude=",
    # The LXC guests' only real interface is nic0; on the bare-metal hosts this
    # also keeps per-VM tap/veth/fwbr interfaces out of the netdev series. The
    # include pattern already omits `lo`, so clearing the exclude above does not
    # reintroduce it.
    "--collector.netdev.device-include=^(nic[0-9]+|eth[0-9]+)$",
)

# Bare metal owns its disks and sensors, so it reports them. (These are all
# default-on collectors; listed explicitly to document intent and to keep the
# rendered ARGS identical to what these hosts ran before templating.)
_NODE_EXPORTER_BAREMETAL_ARGS = (
    "--collector.diskstats",
    "--collector.hwmon",
    "--collector.zfs",
)

# An LXC guest does not own any of it. node_exporter enables its default
# collector set regardless of which --collector.* flags are listed, so the
# host-hardware ones have to be negated explicitly or the guest republishes the
# PVE host's data under its own `host` label -- double-counting anything that
# aggregates across hosts (node_zfs_arc_size alone is referenced ~193 times in
# Grafana). Verified per collector against a live guest:
#   zfs        /proc/spl/kstat/zfs is the host's ARC
#   hwmon      /sys/class/hwmon is the host's sensors
#   diskstats  lxcfs passes the host's disks through (neo showed bray's NVMes,
#              with partitions/loops on top of what bray reports itself)
#   nvme       /sys/class/nvme is the host's controllers
#   thermal_zone, edac  host thermal zones and ECC counters
# cpu/meminfo/loadavg stay: lxcfs virtualises those to the guest's own limits,
# which is exactly what we want to see. filesystem, netdev, systemd and textfile
# are all genuinely guest-scoped.
_NODE_EXPORTER_LXC_ARGS = (
    "--no-collector.zfs",
    "--no-collector.hwmon",
    "--no-collector.diskstats",
    "--no-collector.nvme",
    "--no-collector.thermal_zone",
    "--no-collector.edac",
)

# Single source of truth for what this module manages: build_name (file staged
# under build/<host>/), remote_path, mode, and the feature flag (if any) that
# gates whether the file is part of a given host's file-map at all. install.sh
# derives everything it needs (which packages to check for, which units to
# enable/disable) from the resulting file-map instead of carrying its own copy
# of this list.
FILE_SPECS = (
    # zfs/smartctl need /dev/zfs and real disk device nodes, neither of which
    # exists in an unprivileged LXC guest, so both are bare-metal only. install.sh
    # keys off their presence in the file map, so a guest simply never installs or
    # enables them.
    FileSpec(
        "zfs-pool-textfile-exporter",
        "/usr/local/bin/zfs-pool-textfile-exporter",
        mode="755",
        feature="baremetal",
    ),
    FileSpec(
        "zfs-pool-textfile-exporter.service",
        "/etc/systemd/system/zfs-pool-textfile-exporter.service",
        feature="baremetal",
    ),
    FileSpec(
        "zfs-pool-textfile-exporter.timer",
        "/etc/systemd/system/zfs-pool-textfile-exporter.timer",
        feature="baremetal",
    ),
    # SAS HBA health: controller temperature and per-PHY link state. Bare metal
    # only for the same reason as the ZFS exporter -- it needs /dev/mpt2ctl,
    # /sys/class/scsi_host and /sys/class/sas_phy, none of which an LXC guest
    # owns. Gated on metrics-exporters.hba so the hosts with no HBA (bray,
    # osiris) never install it.
    FileSpec(
        "hba-textfile-exporter.py",
        "/usr/local/bin/hba-textfile-exporter",
        mode="755",
        feature="hba",
    ),
    FileSpec(
        "hba-textfile-exporter.service",
        "/etc/systemd/system/hba-textfile-exporter.service",
        feature="hba",
    ),
    FileSpec(
        "hba-textfile-exporter.timer",
        "/etc/systemd/system/hba-textfile-exporter.timer",
        feature="hba",
    ),
    # Human-readable disk names, derived from ZFS pool/vdev position/capacity so
    # Grafana legends can say "Ace Z1 D1 (20TB)" instead of a raw serial. Bare
    # metal only for the same reason as the ZFS exporter: it reads /sys/block
    # and `zpool status`, and an LXC guest owns neither (lxcfs would show it the
    # PVE host's disks). Replaces ~966 hand-written serial overrides that lived
    # in the dashboard; see the exporter's docstring.
    FileSpec(
        "disk-label-textfile-exporter.py",
        "/usr/local/bin/disk-label-textfile-exporter",
        mode="755",
        feature="baremetal",
    ),
    FileSpec(
        "disk-label-textfile-exporter.service",
        "/etc/systemd/system/disk-label-textfile-exporter.service",
        feature="baremetal",
    ),
    FileSpec(
        "disk-label-textfile-exporter.timer",
        "/etc/systemd/system/disk-label-textfile-exporter.timer",
        feature="baremetal",
    ),
    FileSpec(
        "disk-labels.conf",
        "/etc/homelab/disk-labels.conf",
        feature="disk_label_overrides",
    ),
    # Pending-reboot reporting: whether the running kernel is older than the
    # newest installed one. Bare metal only -- an LXC guest runs the PVE host's
    # kernel and has no kernel packages of its own, so there is nothing it could
    # reboot into. Same `baremetal` gate as the ZFS exporter, which also covers
    # ghost (WSL, flagged lxc_guest) whose kernel comes from Windows, not apt.
    FileSpec(
        "reboot-textfile-exporter",
        "/usr/local/bin/reboot-textfile-exporter",
        mode="755",
        feature="baremetal",
    ),
    FileSpec(
        "reboot-textfile-exporter.service",
        "/etc/systemd/system/reboot-textfile-exporter.service",
        feature="baremetal",
    ),
    FileSpec(
        "reboot-textfile-exporter.timer",
        "/etc/systemd/system/reboot-textfile-exporter.timer",
        feature="baremetal",
    ),
    FileSpec("node-exporter.defaults", "/etc/default/prometheus-node-exporter"),
    # smartctl_exporter itself comes from the distro package
    # (prometheus-smartctl-exporter); we only override the packaged unit's
    # argument-less ExecStart. See README for why apt owns the binary here but
    # igpu-exporter is our own script.
    FileSpec(
        "smartctl-exporter-override.conf",
        "/etc/systemd/system/smartctl_exporter.service.d/override.conf",
        feature="baremetal",
    ),
    FileSpec(
        "smartctl-wrapper.sh",
        SMARTCTL_WRAPPER_BIN,
        mode="755",
        feature="smartctl_wrapper",
    ),
    FileSpec(
        "apcupsd-exporter.py", "/usr/local/bin/apcupsd-exporter", mode="755", feature="apcupsd"
    ),
    FileSpec(
        "apcupsd-exporter.service",
        "/etc/systemd/system/apcupsd-exporter.service",
        feature="apcupsd",
    ),
    FileSpec("apcupsd-exporter.env", "/etc/default/apcupsd-exporter", feature="apcupsd"),
    FileSpec(
        "igpu-exporter.py", "/usr/local/bin/igpu-exporter", mode="755", feature="igpu"
    ),
    FileSpec("igpu-exporter.defaults", "/etc/default/igpu-exporter", feature="igpu"),
    FileSpec("igpu-exporter.service", "/etc/systemd/system/igpu-exporter.service", feature="igpu"),
    FileSpec(
        "zfs-expected-pools.conf",
        "/etc/homelab/zfs-expected-pools.conf",
        feature="zfs_expected_pools",
    ),
)

# apcupsd-exporter.env and zfs-expected-pools.conf are rendered purely from
# templates/ (per-host values, no static common_dir counterpart), so they're
# excluded from the repo-file existence check in validate().
_TEMPLATED_ONLY_BUILD_NAMES = {
    "apcupsd-exporter.env",
    "zfs-expected-pools.conf",
    "disk-labels.conf",
    "smartctl-exporter-override.conf",
    "node-exporter.defaults",
}
REQUIRED = [
    spec.build_name for spec in FILE_SPECS if spec.build_name not in _TEMPLATED_ONLY_BUILD_NAMES
]


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return run_module_deploy(
        root,
        requested_host,
        "metrics-exporters",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, _hosts: validate(root),
    )


def validate(root: Path) -> None:
    common_dir = root / "metrics-exporters" / "configs" / "common"
    if not common_dir.is_dir():
        raise ValueError(f"configs/common not found: {common_dir}")
    for name in REQUIRED:
        if not (common_dir / name).is_file():
            raise ValueError(f"Missing required config: {common_dir / name}")
    template = apcupsd_exporter_env_template(root)
    if not template.is_file():
        raise ValueError(f"Missing required config: {template}")
    pools_template = zfs_expected_pools_template(root)
    if not pools_template.is_file():
        raise ValueError(f"Missing required config: {pools_template}")
    overrides_template = disk_labels_template(root)
    if not overrides_template.is_file():
        raise ValueError(f"Missing required config: {overrides_template}")
    for template in (smartctl_override_template(root), node_exporter_defaults_template(root)):
        if not template.is_file():
            raise ValueError(f"Missing required config: {template}")


def is_lxc_guest(root: Path, host: str) -> bool:
    """Whether this host is an LXC guest rather than bare metal.

    Guests share the PVE host's kernel: no /dev/zfs, no disk device nodes, and
    /proc/spl/kstat/zfs plus /sys/class/hwmon belong to the host. So they get a
    reduced collector set and neither the ZFS textfile exporter nor
    smartctl_exporter.
    """
    registry = default_registry(root)
    return normalize_bool(
        registry.get(host, "metrics-exporters.lxc_guest", None),
        False,
        f"metrics-exporters.lxc_guest must be true or false for {host}",
    )


def node_exporter_args(*, lxc_guest: bool) -> str:
    extra = _NODE_EXPORTER_LXC_ARGS if lxc_guest else _NODE_EXPORTER_BAREMETAL_ARGS
    return " ".join((*_NODE_EXPORTER_COMMON_ARGS, *extra))


def has_smartctl_wrapper(root: Path, host: str) -> bool:
    registry = default_registry(root)
    return normalize_bool(
        registry.get(host, "metrics-exporters.smartctl_wrapper", None),
        False,
        f"metrics-exporters.smartctl_wrapper must be true or false for {host}",
    )


def has_apcupsd_exporter(root: Path, host: str) -> bool:
    registry = default_registry(root)
    role = str(registry.get(host, "apcupsd.role", "none"))
    return role in {"master", "master-standalone"}


def has_hba_exporter(root: Path, host: str) -> bool:
    registry = default_registry(root)
    return normalize_bool(
        registry.get(host, "metrics-exporters.hba", None),
        False,
        f"metrics-exporters.hba must be true or false for {host}",
    )


def has_igpu_exporter(root: Path, host: str) -> bool:
    registry = default_registry(root)
    exporter_config = registry.get(host, "metrics-exporters.intel_gpu_exporter", None)
    return isinstance(exporter_config, dict)


def apcupsd_exporter_env_template(root: Path) -> Path:
    return root / "metrics-exporters" / "templates" / "apcupsd-exporter.env.tpl"


def zfs_expected_pools_template(root: Path) -> Path:
    return root / "metrics-exporters" / "templates" / "zfs-expected-pools.conf.tpl"


def disk_labels_template(root: Path) -> Path:
    return root / "metrics-exporters" / "templates" / "disk-labels.conf.tpl"


def smartctl_override_template(root: Path) -> Path:
    return root / "metrics-exporters" / "templates" / "smartctl-exporter-override.conf.tpl"


def node_exporter_defaults_template(root: Path) -> Path:
    return root / "metrics-exporters" / "templates" / "node-exporter.defaults.tpl"


def zfs_expected_pools(root: Path, host: str) -> list[str]:
    registry = default_registry(root)
    configured = registry.get(host, "metrics-exporters.zfs_expected_pools", None)
    if configured is None:
        return []
    if not isinstance(configured, list):
        raise ValueError(
            f"{host}: metrics-exporters.zfs_expected_pools must be a list of pool names"
        )
    pools: list[str] = []
    for entry in configured:
        pool = str(entry).strip()
        if not pool:
            raise ValueError(
                f"{host}: metrics-exporters.zfs_expected_pools contains an empty entry"
            )
        pools.append(pool)
    return pools


def disk_label_overrides(root: Path, host: str) -> list[tuple[str, str]]:
    """Per-model display-name overrides for disk-label-textfile-exporter.

    The exporter derives every name from the running system, so this is only for
    the rare disk whose useful name is not derivable -- a USB enclosure whose
    model string names the bare drive inside it. Keyed by model rather than by
    serial on purpose: a model is not a hardware identifier, so it is safe in a
    public repo.
    """
    registry = default_registry(root)
    configured = registry.get(host, "metrics-exporters.disk_labels", None)
    if configured is None:
        return []
    if not isinstance(configured, dict):
        raise ValueError(
            f"{host}: metrics-exporters.disk_labels must be a mapping of model to name"
        )
    overrides: list[tuple[str, str]] = []
    for model, name in configured.items():
        model_text, name_text = str(model).strip(), str(name).strip()
        if not model_text or not name_text:
            raise ValueError(f"{host}: metrics-exporters.disk_labels has an empty model or name")
        if "=" in model_text or "\n" in name_text:
            raise ValueError(
                f"{host}: metrics-exporters.disk_labels model must not contain '=' "
                f"and name must be a single line ({model_text!r})"
            )
        overrides.append((model_text, name_text))
    return overrides


def build_file_specs(
    *,
    has_apcupsd: bool,
    has_igpu: bool,
    has_hba: bool,
    has_expected_pools: bool,
    has_disk_label_overrides: bool,
    has_wrapper: bool,
    lxc_guest: bool,
) -> tuple[FileSpec, ...]:
    enabled_features = {
        "baremetal": not lxc_guest,
        "apcupsd": has_apcupsd,
        "igpu": has_igpu,
        "hba": has_hba and not lxc_guest,
        "zfs_expected_pools": has_expected_pools and not lxc_guest,
        "disk_label_overrides": has_disk_label_overrides and not lxc_guest,
        "smartctl_wrapper": has_wrapper and not lxc_guest,
    }
    return tuple(
        spec
        for spec in FILE_SPECS
        if spec.feature is None or enabled_features.get(spec.feature, False)
    )


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    common_dir = root / "metrics-exporters" / "configs" / "common"
    build_dir = root / "metrics-exporters" / "build" / host
    prepare_build_dir(build_dir)

    lxc_guest = is_lxc_guest(root, host)
    render_file(
        node_exporter_defaults_template(root),
        build_dir / "node-exporter.defaults",
        NODE_EXPORTER_ARGS=node_exporter_args(lxc_guest=lxc_guest),
    )

    has_wrapper = has_smartctl_wrapper(root, host) and not lxc_guest
    if not lxc_guest:
        copy_files(common_dir, build_dir, [
            "zfs-pool-textfile-exporter",
            "zfs-pool-textfile-exporter.service",
            "zfs-pool-textfile-exporter.timer",
            "reboot-textfile-exporter",
            "reboot-textfile-exporter.service",
            "reboot-textfile-exporter.timer",
            "disk-label-textfile-exporter.py",
            "disk-label-textfile-exporter.service",
            "disk-label-textfile-exporter.timer",
        ])
        if has_wrapper:
            copy_files(common_dir, build_dir, ["smartctl-wrapper.sh"])
        render_file(
            smartctl_override_template(root),
            build_dir / "smartctl-exporter-override.conf",
            SMARTCTL_PATH=SMARTCTL_WRAPPER_BIN if has_wrapper else SMARTCTL_BIN,
        )

    connection = HostConnection(
        host,
        user=str(registry.get(host, "config.user")),
        hostname=str(registry.get(host, "config.hostname")),
    )

    has_apcupsd = has_apcupsd_exporter(root, host)
    if has_apcupsd:
        try:
            upsname = str(registry.get(host, "apcupsd.name"))
        except HostLookupError as exc:
            raise ValueError(str(exc)) from exc
        serial = ""
        if not dry_run:
            result = connection.connection.run(
                (
                    "apcaccess status 2>/dev/null | sed -n "
                    "'s/^SERIALNO[[:space:]]*:[[:space:]]*//p' | xargs"
                ),
                warn=True,
                hide=True,
            )
            serial = result.stdout.strip()
        copy_files(common_dir, build_dir, ["apcupsd-exporter.py", "apcupsd-exporter.service"])
        render_file(
            apcupsd_exporter_env_template(root),
            build_dir / "apcupsd-exporter.env",
            UPS_NAME=upsname,
            UPS_HOST=host,
            UPS_SERIAL=serial,
        )

    has_igpu = has_igpu_exporter(root, host)
    if has_igpu:
        port = int(registry.get(host, "metrics-exporters.intel_gpu_exporter.port", 9400))
        refresh_period_ms = int(
            registry.get(host, "metrics-exporters.intel_gpu_exporter.refresh_period_ms", 1000)
        )
        device = str(registry.get(host, "metrics-exporters.intel_gpu_exporter.device", "")).strip()
        render_file(
            common_dir / "igpu-exporter.defaults",
            build_dir / "igpu-exporter.defaults",
            IGPU_EXPORTER_PORT=str(port),
            IGPU_EXPORTER_REFRESH_PERIOD_MS=str(refresh_period_ms),
            IGPU_EXPORTER_DEVICE=device,
        )
        copy_files(common_dir, build_dir, ["igpu-exporter.py", "igpu-exporter.service"])

    has_hba = has_hba_exporter(root, host) and not lxc_guest
    if has_hba:
        copy_files(common_dir, build_dir, [
            "hba-textfile-exporter.py",
            "hba-textfile-exporter.service",
            "hba-textfile-exporter.timer",
        ])

    expected_pools = [] if lxc_guest else zfs_expected_pools(root, host)
    if expected_pools:
        render_file(
            zfs_expected_pools_template(root),
            build_dir / "zfs-expected-pools.conf",
            ZFS_EXPECTED_POOLS=expected_pools,
        )

    label_overrides = [] if lxc_guest else disk_label_overrides(root, host)
    if label_overrides:
        render_file(
            disk_labels_template(root),
            build_dir / "disk-labels.conf",
            DISK_LABEL_OVERRIDES=label_overrides,
        )

    file_specs = build_file_specs(
        has_apcupsd=has_apcupsd,
        has_igpu=has_igpu,
        has_hba=has_hba,
        has_expected_pools=bool(expected_pools),
        has_disk_label_overrides=bool(label_overrides),
        has_wrapper=has_wrapper,
        lxc_guest=lxc_guest,
    )
    write_file_map(build_dir, file_specs)

    print_sub("Comparing with remote configs...")
    diffs = [(build_dir / spec.build_name, spec.remote_path) for spec in file_specs]
    for message in diff_many(connection, diffs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "metrics-exporters" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
