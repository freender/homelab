from __future__ import annotations

from pathlib import Path

from ..build import copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import FileSpec, write_file_map
from ..output import print_action, print_sub
from ..ssh import HostConnection, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-exporters"
ALLOWED_RUNTIMES = {"native", "docker"}

# Single source of truth for what this module manages: build_name (file staged
# under build/<host>/), remote_path, mode, and the feature flag (if any) that
# gates whether the file is part of a given host's file-map at all. install.sh
# derives everything it needs (which packages to check for, which units to
# enable/disable, which binaries to fetch) from the resulting file-map instead
# of carrying its own copy of this list.
FILE_SPECS = (
    FileSpec("zfs-pool-textfile-exporter", "/usr/local/bin/zfs-pool-textfile-exporter", mode="755"),
    FileSpec(
        "zfs-pool-textfile-exporter.service",
        "/etc/systemd/system/zfs-pool-textfile-exporter.service",
    ),
    FileSpec(
        "zfs-pool-textfile-exporter.timer",
        "/etc/systemd/system/zfs-pool-textfile-exporter.timer",
    ),
    FileSpec("node-exporter.defaults", "/etc/default/prometheus-node-exporter", feature="native"),
    # smartctl_exporter itself comes from the distro package
    # (prometheus-smartctl-exporter, <codename>-backports); we only override the
    # packaged unit's argument-less ExecStart. See README for why apt owns the
    # binary here but igpu-exporter is still built from source.
    FileSpec(
        "smartctl-exporter-override.conf",
        "/etc/systemd/system/smartctl_exporter.service.d/override.conf",
        feature="native",
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
_TEMPLATED_ONLY_BUILD_NAMES = {"apcupsd-exporter.env", "zfs-expected-pools.conf"}
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
    supported_hosts = registry.list_hosts(feature="pve-exporters")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-exporters (not applicable to {requested_host})")
        return 0
    validate(root)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    common_dir = root / "pve-exporters" / "configs" / "common"
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


def exporter_runtime(root: Path, host: str) -> str:
    registry = default_registry(root)
    runtime = str(registry.get(host, "pve-exporters.runtime", "native"))
    if runtime not in ALLOWED_RUNTIMES:
        raise ValueError(
            f"{host}: pve-exporters.runtime must be one of "
            f"{sorted(ALLOWED_RUNTIMES)}, got {runtime!r}"
        )
    return runtime


def has_apcupsd_exporter(root: Path, host: str) -> bool:
    registry = default_registry(root)
    role = str(registry.get(host, "apcupsd.role", "none"))
    return role in {"master", "master-standalone"}


def has_igpu_exporter(root: Path, host: str) -> bool:
    registry = default_registry(root)
    exporter_config = registry.get(host, "pve-exporters.intel_gpu_exporter", None)
    return isinstance(exporter_config, dict)


def apcupsd_exporter_env_template(root: Path) -> Path:
    return root / "pve-exporters" / "templates" / "apcupsd-exporter.env.tpl"


def zfs_expected_pools_template(root: Path) -> Path:
    return root / "pve-exporters" / "templates" / "zfs-expected-pools.conf.tpl"


def zfs_expected_pools(root: Path, host: str) -> list[str]:
    registry = default_registry(root)
    configured = registry.get(host, "pve-exporters.zfs_expected_pools", None)
    if configured is None:
        return []
    if not isinstance(configured, list):
        raise ValueError(
            f"{host}: pve-exporters.zfs_expected_pools must be a list of pool names"
        )
    pools: list[str] = []
    for entry in configured:
        pool = str(entry).strip()
        if not pool:
            raise ValueError(f"{host}: pve-exporters.zfs_expected_pools contains an empty entry")
        pools.append(pool)
    return pools


def build_file_specs(
    *, runtime: str, has_apcupsd: bool, has_igpu: bool, has_expected_pools: bool
) -> tuple[FileSpec, ...]:
    enabled_features = {
        "native": runtime != "docker",
        "apcupsd": has_apcupsd,
        "igpu": has_igpu,
        "zfs_expected_pools": has_expected_pools,
    }
    return tuple(
        spec
        for spec in FILE_SPECS
        if spec.feature is None or enabled_features.get(spec.feature, False)
    )


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    runtime = exporter_runtime(root, host)
    common_dir = root / "pve-exporters" / "configs" / "common"
    build_dir = root / "pve-exporters" / "build" / host
    prepare_build_dir(build_dir)

    copy_files(common_dir, build_dir, [
        "zfs-pool-textfile-exporter",
        "zfs-pool-textfile-exporter.service",
        "zfs-pool-textfile-exporter.timer",
    ])
    # native node-exporter/smartctl-exporter are not managed in docker mode: the
    # host-managed compose stack owns those services instead (see README). Skip
    # staging them so the file-map (and install.sh) has nothing to install for
    # these files, and a leftover unit from an earlier native deploy gets
    # actively cleaned up instead of merely installed-then-disabled.
    if runtime != "docker":
        copy_files(common_dir, build_dir, [
            "node-exporter.defaults",
            "smartctl-exporter-override.conf",
        ])

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
        port = int(registry.get(host, "pve-exporters.intel_gpu_exporter.port", 9400))
        refresh_period_ms = int(
            registry.get(host, "pve-exporters.intel_gpu_exporter.refresh_period_ms", 1000)
        )
        device = str(registry.get(host, "pve-exporters.intel_gpu_exporter.device", "")).strip()
        render_file(
            common_dir / "igpu-exporter.defaults",
            build_dir / "igpu-exporter.defaults",
            IGPU_EXPORTER_PORT=str(port),
            IGPU_EXPORTER_REFRESH_PERIOD_MS=str(refresh_period_ms),
            IGPU_EXPORTER_DEVICE=device,
        )
        copy_files(common_dir, build_dir, ["igpu-exporter.py", "igpu-exporter.service"])

    expected_pools = zfs_expected_pools(root, host)
    if expected_pools:
        render_file(
            zfs_expected_pools_template(root),
            build_dir / "zfs-expected-pools.conf",
            ZFS_EXPECTED_POOLS=expected_pools,
        )

    file_specs = build_file_specs(
        runtime=runtime,
        has_apcupsd=has_apcupsd,
        has_igpu=has_igpu,
        has_expected_pools=bool(expected_pools),
    )
    write_file_map(build_dir, file_specs)

    print_sub("Comparing with remote configs...")
    diffs = [(build_dir / spec.build_name, spec.remote_path) for spec in file_specs]
    for message in diff_many(connection, diffs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        return

    env = force_env(force)
    env["EXPORTER_RUNTIME"] = runtime

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-exporters" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=env,
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
