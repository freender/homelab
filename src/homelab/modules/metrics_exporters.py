from __future__ import annotations

from pathlib import Path

from ..build import copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import FileSpec, normalize_bool, write_file_map
from ..output import print_action, print_sub
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
    "--collector.netdev",
    "--collector.textfile",
    f"--collector.textfile.directory={TEXTFILE_DIR}",
    "--collector.systemd",
    "--collector.uname",
    "--no-collector.xfs",
    # The LXC guests' only real interface is nic0; on the bare-metal hosts this
    # also keeps per-VM tap/veth/fwbr interfaces out of the netdev series.
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
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="metrics-exporters")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping metrics-exporters (not applicable to {requested_host})")
        return 0
    validate(root)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


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


def has_igpu_exporter(root: Path, host: str) -> bool:
    registry = default_registry(root)
    exporter_config = registry.get(host, "metrics-exporters.intel_gpu_exporter", None)
    return isinstance(exporter_config, dict)


def apcupsd_exporter_env_template(root: Path) -> Path:
    return root / "metrics-exporters" / "templates" / "apcupsd-exporter.env.tpl"


def zfs_expected_pools_template(root: Path) -> Path:
    return root / "metrics-exporters" / "templates" / "zfs-expected-pools.conf.tpl"


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


def build_file_specs(
    *,
    has_apcupsd: bool,
    has_igpu: bool,
    has_expected_pools: bool,
    has_wrapper: bool,
    lxc_guest: bool,
) -> tuple[FileSpec, ...]:
    enabled_features = {
        "baremetal": not lxc_guest,
        "apcupsd": has_apcupsd,
        "igpu": has_igpu,
        "zfs_expected_pools": has_expected_pools and not lxc_guest,
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

    expected_pools = [] if lxc_guest else zfs_expected_pools(root, host)
    if expected_pools:
        render_file(
            zfs_expected_pools_template(root),
            build_dir / "zfs-expected-pools.conf",
            ZFS_EXPECTED_POOLS=expected_pools,
        )

    file_specs = build_file_specs(
        has_apcupsd=has_apcupsd,
        has_igpu=has_igpu,
        has_expected_pools=bool(expected_pools),
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
