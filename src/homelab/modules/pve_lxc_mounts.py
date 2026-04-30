from __future__ import annotations

from pathlib import Path

from invoke.exceptions import UnexpectedExit

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..media_storage import load_media_storage
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, offline_mode

REMOTE_ROOT = "/tmp/homelab-pve-lxc-mounts"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-lxc-mounts")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-lxc-mounts (not applicable to {requested_host})")
        return 0

    try:
        validate(root, hosts)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    installer = root / "pve-lxc-mounts" / "scripts" / "install.sh"
    if not installer.is_file():
        raise ValueError(f"missing installer: {installer}")

    registry = default_registry(root)
    for host in hosts:
        if str(registry.get(host, "config.type")) != "pve":
            raise ValueError(
                f"Unsupported host type for {host}: {registry.get(host, 'config.type')}"
            )

        containers = registry.get(host, "pve-lxc-mounts.containers", [])
        if not isinstance(containers, list) or not containers:
            raise ValueError(f"{host}: pve-lxc-mounts.containers must be a non-empty list")

        for index, container in enumerate(containers):
            if not isinstance(container, dict):
                raise ValueError(f"{host}: invalid container definition at index {index}")

            ctid = str(container.get("ctid", "")).strip()
            if not ctid.isdigit():
                raise ValueError(f"{host}: invalid ctid at index {index}")

            features = container.get("features", {})
            if features is not None and not isinstance(features, dict):
                raise ValueError(f"{host}: {ctid} features must be a mapping")
            if isinstance(features, dict):
                for feature_name, feature_value in features.items():
                    if not str(feature_name).strip():
                        raise ValueError(f"{host}: {ctid} feature names must be non-empty")
                    if str(feature_value).lower() not in {"0", "1", "false", "true"}:
                        raise ValueError(
                            f"{host}: {ctid} feature {feature_name} must be 0/1/false/true"
                        )

            root_mounts = container.get("root_mounts", [])
            if not isinstance(root_mounts, list):
                raise ValueError(f"{host}: {ctid} root_mounts must be a list")

            seen_slots: set[int] = set()
            for mount_index, mount in enumerate(root_mounts):
                if not isinstance(mount, dict):
                    raise ValueError(f"{host}: {ctid} invalid root mount at index {mount_index}")

                try:
                    slot = int(mount.get("slot"))
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{host}: {ctid} invalid slot for root mount {mount_index}"
                    ) from exc

                source = str(mount.get("source", "")).strip()
                target = str(mount.get("target", "")).strip()
                backup = str(mount.get("backup", "")).strip()
                if not is_valid_root_mount_source(source):
                    raise ValueError(
                        f"{host}: {ctid} source must be absolute or a Proxmox volume "
                        f"for root mount {mount_index}"
                    )
                if not target.startswith("/mnt/"):
                    raise ValueError(
                        f"{host}: {ctid} target must live under /mnt for root mount {mount_index}"
                    )
                if backup not in {"0", "1"}:
                    raise ValueError(
                        f"{host}: {ctid} backup must be 0 or 1 for root mount {mount_index}"
                    )
                if slot in seen_slots:
                    raise ValueError(f"{host}: {ctid} duplicate root mount slot {slot}")
                seen_slots.add(slot)

            idmapped_roots = container.get("idmapped_roots", [])
            if not isinstance(idmapped_roots, list):
                raise ValueError(f"{host}: {ctid} idmapped_roots must be a list")
            for root_path in idmapped_roots:
                if not str(root_path).startswith("/"):
                    raise ValueError(
                        f"{host}: {ctid} idmapped root must be absolute: {root_path}"
                    )

            prune_idmapped_roots = container.get("prune_idmapped_roots", [])
            if not isinstance(prune_idmapped_roots, list):
                raise ValueError(f"{host}: {ctid} prune_idmapped_roots must be a list")
            for root_path in prune_idmapped_roots:
                if not str(root_path).startswith("/"):
                    raise ValueError(
                        f"{host}: {ctid} prune idmapped root must be absolute: {root_path}"
                    )

            use_idmapped_mounts = container.get("use_idmapped_mounts", True)
            if str(use_idmapped_mounts).lower() not in {"0", "1", "false", "true"}:
                raise ValueError(f"{host}: {ctid} use_idmapped_mounts must be 0/1/false/true")

            idmapped_mounts = container.get("idmapped_mounts", [])
            if not isinstance(idmapped_mounts, list):
                raise ValueError(f"{host}: {ctid} idmapped_mounts must be a list")
            if str(container.get("export_media_storage", "")).lower() in {"1", "true"}:
                media_storage_host = str(container.get("media_storage_target_host", host)).strip()
                if not media_storage_host:
                    raise ValueError(f"{host}: {ctid} media_storage_target_host must be non-empty")
                media_storage = load_media_storage(registry, media_storage_host)
                exported_mounts = [
                    {"source": source, "target": target}
                    for source, target in (
                        () if media_storage is None else media_storage.export_idmapped_mounts()
                    )
                ]
                if not exported_mounts:
                    raise ValueError(
                        f"{host}: {ctid} export_media_storage resolved no media mounts"
                    )
                idmapped_mounts = [*idmapped_mounts, *exported_mounts]
            if not root_mounts and not idmapped_mounts:
                raise ValueError(
                    f"{host}: {ctid} must define at least one root_mount or idmapped_mount"
                )
            for mount_index, mount in enumerate(idmapped_mounts):
                if not isinstance(mount, dict):
                    raise ValueError(
                        f"{host}: {ctid} invalid idmapped mount at index {mount_index}"
                    )

                source = str(mount.get("source", "")).strip()
                target = str(mount.get("target", "")).strip()
                if not source.startswith("/"):
                    raise ValueError(
                        f"{host}: {ctid} source must be absolute for idmapped mount {mount_index}"
                    )
                if not target.startswith("/mnt/"):
                    raise ValueError(
                        f"{host}: {ctid} target must live under /mnt "
                        f"for idmapped mount {mount_index}"
                    )

def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    build_dir = root / "pve-lxc-mounts" / "build" / host
    prepare_build_dir(build_dir)

    containers = registry.get(host, "pve-lxc-mounts.containers", [])
    connection = HostConnection(host)
    print_sub("Rendering desired LXC config from live ZFS datasets...")

    if dry_run and offline_mode():
        print_sub(
            "[DRY-RUN] Offline mode: skipping live config rendering (requires remote access)"
        )
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        return

    container_ids: list[str] = []
    for container in containers:
        ctid = str(container["ctid"])
        root_mounts = normalize_root_mounts(container["root_mounts"])
        idmapped_roots = [str(path) for path in container.get("idmapped_roots", [])]
        prune_idmapped_roots = [
            str(path) for path in container.get("prune_idmapped_roots", [])
        ]
        idmapped_exclude = [str(path) for path in container.get("idmapped_exclude", [])]
        use_idmapped_mounts = str(container.get("use_idmapped_mounts", True)).lower() in {
            "1",
            "true",
        }
        idmapped_mounts_raw = container.get("idmapped_mounts", [])
        if str(container.get("export_media_storage", "")).lower() in {
            "1",
            "true",
        }:
            media_storage_host = str(container.get("media_storage_target_host", host)).strip()
            if not media_storage_host:
                raise ValueError(f"{host}: {ctid} media_storage_target_host must be non-empty")
            media_storage = load_media_storage(registry, media_storage_host)
            exported_mounts = [
                {"source": source, "target": target}
                for source, target in (
                    () if media_storage is None else media_storage.export_idmapped_mounts()
                )
            ]
            if not exported_mounts:
                raise ValueError(f"{host}: {ctid} export_media_storage resolved no media mounts")
            idmapped_mounts_raw = [*idmapped_mounts_raw, *exported_mounts]
        idmapped_mounts = normalize_idmapped_mounts(idmapped_mounts_raw)
        features = normalize_features(container.get("features", {}))
        leaf_roots = idmapped_roots

        current_config = read_remote_text(connection, f"/etc/pve/lxc/{ctid}.conf")
        leaf_mounts = query_leaf_mounts(connection, leaf_roots, idmapped_exclude)
        desired_config = render_config(
            current_config,
            root_mounts,
            idmapped_roots,
            prune_idmapped_roots,
            leaf_mounts,
            idmapped_mounts,
            features,
            use_idmapped_mounts,
        )

        local_config = build_dir / f"{ctid}.conf"
        local_config.write_text(desired_config, encoding="utf-8")
        container_ids.append(ctid)

        _, message = connection.remote_diff(local_config, f"/etc/pve/lxc/{ctid}.conf")
        print_sub(message)
        print_sub(f"Leaf datasets: {len(leaf_mounts)}")

    (build_dir / "containers.conf").write_text("\n".join(container_ids) + "\n", encoding="utf-8")

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-lxc-mounts" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )


def normalize_root_mounts(root_mounts: list[dict[str, object]]) -> list[dict[str, str]]:
    mounts: list[dict[str, str]] = []
    for mount in sorted(root_mounts, key=lambda item: int(item["slot"])):
        mounts.append(
            {
                "slot": str(int(mount["slot"])),
                "source": str(mount["source"]),
                "target": str(mount["target"]),
                "backup": str(mount["backup"]),
            }
        )
    return mounts


def is_valid_root_mount_source(source: str) -> bool:
    if source.startswith("/"):
        return True
    if any(char.isspace() for char in source) or "," in source:
        return False
    storage, separator, volume = source.partition(":")
    return bool(storage and separator and volume)


def normalize_features(features: dict[str, object]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in sorted(features.items()):
        normalized[str(name)] = "1" if str(value).lower() in {"1", "true"} else "0"
    return normalized


def normalize_idmapped_mounts(idmapped_mounts: list[dict[str, object]]) -> list[dict[str, str]]:
    mounts: list[dict[str, str]] = []
    for mount in idmapped_mounts:
        mounts.append(
            {
                "source": str(mount["source"]),
                "target": str(mount["target"]),
            }
        )
    return mounts


def read_remote_text(connection: HostConnection, remote_path: str) -> str:
    if offline_mode():
        raise ValueError(f"offline mode cannot render remote config: {remote_path}")

    try:
        result = connection.connection.run(f'cat "{remote_path}"', hide=True)
    except UnexpectedExit as exc:
        raise ValueError(f"unable to read remote config: {remote_path}") from exc
    return result.stdout


def query_leaf_mounts(
    connection: HostConnection,
    roots: list[str],
    exclude_roots: list[str],
) -> list[str]:
    if offline_mode():
        raise ValueError("offline mode cannot query remote ZFS datasets")

    result = connection.connection.run("zfs list -H -o mountpoint", hide=True)
    mountpoints = [
        line.strip() for line in result.stdout.splitlines() if line.strip().startswith("/")
    ]
    relevant = sorted(
        {
            mountpoint
            for mountpoint in mountpoints
            if any(mountpoint == root or mountpoint.startswith(f"{root}/") for root in roots)
            and not any(
                mountpoint == exclude_root or mountpoint.startswith(f"{exclude_root}/")
                for exclude_root in exclude_roots
            )
        }
    )
    return [
        mountpoint
        for mountpoint in relevant
        if mountpoint not in roots
        and not any(other.startswith(f"{mountpoint}/") for other in relevant)
    ]


def render_config(
    current_config: str,
    root_mounts: list[dict[str, str]],
    idmapped_roots: list[str],
    prune_idmapped_roots: list[str],
    leaf_mounts: list[str],
    idmapped_mounts: list[dict[str, str]],
    features: dict[str, str],
    use_idmapped_mounts: bool,
) -> str:
    managed_slots = {f"mp{mount['slot']}:" for mount in root_mounts}
    managed_root_sources = {mount["source"] for mount in root_mounts}
    managed_root_targets = {mount["target"] for mount in root_mounts}
    feature_line = None
    if features:
        feature_line = "features: " + ",".join(
            f"{name}={value}" for name, value in features.items()
        )
    explicit_idmapped_sources = {mount["source"] for mount in idmapped_mounts}
    explicit_idmapped_targets = {mount["target"] for mount in idmapped_mounts}
    root_mount_lines = [
        f"mp{mount['slot']}: {mount['source']},mp={mount['target']},backup={mount['backup']}"
        for mount in root_mounts
    ]
    idmap_suffix = ",idmap=container 0 0" if use_idmapped_mounts else ""
    idmapped_lines = [
        f"lxc.mount.entry: {mountpoint} mnt/{mountpoint.lstrip('/')}"
        " none bind,create=dir"
        + idmap_suffix
        for mountpoint in leaf_mounts
        if mountpoint not in explicit_idmapped_sources and mountpoint not in managed_root_sources
        and f"/mnt/{mountpoint.lstrip('/')}" not in managed_root_targets
    ] + [
        f"lxc.mount.entry: {mount['source']} {mount['target'].lstrip('/')}"
        " none bind,create=dir"
        + idmap_suffix
        for mount in idmapped_mounts
    ]
    managed_idmapped_sources = {
        *idmapped_roots,
        *prune_idmapped_roots,
        *(mount["source"] for mount in idmapped_mounts),
    }

    rendered_lines: list[str] = []
    root_mounts_inserted = False
    idmapped_inserted = False
    for line in current_config.splitlines():
        stripped = line.strip()
        if stripped.startswith("features: ") and feature_line is not None:
            rendered_lines.append(feature_line)
            continue
        if stripped.startswith("mp") and ": " in stripped and ",mp=" in stripped:
            target = stripped.split(",mp=", 1)[1].split(",", 1)[0].strip()
            if target in managed_root_targets or target in explicit_idmapped_targets:
                if not root_mounts_inserted and root_mount_lines:
                    rendered_lines.extend(root_mount_lines)
                    root_mounts_inserted = True
                continue
        if any(stripped.startswith(slot) for slot in managed_slots):
            if not root_mounts_inserted:
                rendered_lines.extend(root_mount_lines)
                root_mounts_inserted = True
            continue
        if stripped.startswith("lxc.mount.entry: "):
            parts = stripped.split()
            source = parts[1]
            target = "/" + parts[2].lstrip("/") if len(parts) > 2 else ""
            if source in managed_root_sources:
                continue
            if target in explicit_idmapped_targets:
                if not idmapped_inserted:
                    rendered_lines.extend(idmapped_lines)
                    idmapped_inserted = True
                continue
            if any(
                source == root or source.startswith(f"{root}/")
                for root in managed_idmapped_sources
            ):
                if not idmapped_inserted:
                    rendered_lines.extend(idmapped_lines)
                    idmapped_inserted = True
                continue
        rendered_lines.append(line)

    if not root_mounts_inserted:
        rendered_lines.extend(root_mount_lines)
    if not idmapped_inserted:
        rendered_lines.extend(idmapped_lines)
    if feature_line is not None and not any(
        line.startswith("features: ") for line in rendered_lines
    ):
        rendered_lines.append(feature_line)

    while rendered_lines and not rendered_lines[-1].strip():
        rendered_lines.pop()

    return "\n".join([*rendered_lines, ""])
