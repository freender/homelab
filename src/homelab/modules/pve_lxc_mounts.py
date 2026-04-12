from __future__ import annotations

from pathlib import Path

from invoke.exceptions import UnexpectedExit

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
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
        validate(root, supported_hosts)
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
            raise ValueError(f"Unsupported host type for {host}: {registry.get(host, 'config.type')}")

        containers = registry.get(host, "pve-lxc-mounts.containers", [])
        if not isinstance(containers, list) or not containers:
            raise ValueError(f"{host}: pve-lxc-mounts.containers must be a non-empty list")

        for index, container in enumerate(containers):
            if not isinstance(container, dict):
                raise ValueError(f"{host}: invalid container definition at index {index}")

            ctid = str(container.get("ctid", "")).strip()
            if not ctid.isdigit():
                raise ValueError(f"{host}: invalid ctid at index {index}")

            root_mounts = container.get("root_mounts", [])
            if not isinstance(root_mounts, list) or not root_mounts:
                raise ValueError(f"{host}: {ctid} root_mounts must be a non-empty list")

            seen_slots: set[int] = set()
            for mount_index, mount in enumerate(root_mounts):
                if not isinstance(mount, dict):
                    raise ValueError(f"{host}: {ctid} invalid root mount at index {mount_index}")

                try:
                    slot = int(mount.get("slot"))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{host}: {ctid} invalid slot for root mount {mount_index}") from exc

                source = str(mount.get("source", "")).strip()
                target = str(mount.get("target", "")).strip()
                backup = str(mount.get("backup", "")).strip()
                if not source.startswith("/"):
                    raise ValueError(f"{host}: {ctid} source must be absolute for root mount {mount_index}")
                if not target.startswith("/mnt/"):
                    raise ValueError(f"{host}: {ctid} target must live under /mnt for root mount {mount_index}")
                if backup not in {"0", "1"}:
                    raise ValueError(f"{host}: {ctid} backup must be 0 or 1 for root mount {mount_index}")
                if slot in seen_slots:
                    raise ValueError(f"{host}: {ctid} duplicate root mount slot {slot}")
                seen_slots.add(slot)

            idmapped_roots = container.get("idmapped_roots", [])
            if not isinstance(idmapped_roots, list) or not idmapped_roots:
                raise ValueError(f"{host}: {ctid} idmapped_roots must be a non-empty list")
            for root_path in idmapped_roots:
                if not str(root_path).startswith("/"):
                    raise ValueError(f"{host}: {ctid} idmapped root must be absolute: {root_path}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    build_dir = root / "pve-lxc-mounts" / "build" / host
    prepare_build_dir(build_dir)

    containers = registry.get(host, "pve-lxc-mounts.containers", [])
    connection = HostConnection(host)
    print_sub("Rendering desired LXC config from live ZFS datasets...")

    container_ids: list[str] = []
    for container in containers:
        ctid = str(container["ctid"])
        root_mounts = normalize_root_mounts(container["root_mounts"])
        idmapped_roots = [str(path) for path in container["idmapped_roots"]]

        current_config = read_remote_text(connection, f"/etc/pve/lxc/{ctid}.conf")
        leaf_mounts = query_leaf_mounts(connection, idmapped_roots)
        desired_config = render_config(current_config, root_mounts, idmapped_roots, leaf_mounts)

        local_config = build_dir / f"{ctid}.conf"
        local_config.write_text(desired_config, encoding="utf-8")
        container_ids.append(ctid)

        _, message = connection.remote_diff(local_config, f"/etc/pve/lxc/{ctid}.conf")
        print_sub(message)
        print_sub(f"Leaf datasets: {len(leaf_mounts)}")

    (build_dir / "containers.conf").write_text("\n".join(container_ids) + "\n", encoding="utf-8")

    if dry_run:
        if offline_mode():
            print_sub("[DRY-RUN] Offline mode enabled; live config rendering requires remote access")
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


def read_remote_text(connection: HostConnection, remote_path: str) -> str:
    if offline_mode():
        raise ValueError(f"offline mode cannot render remote config: {remote_path}")

    try:
        result = connection.connection.run(f'cat "{remote_path}"', hide=True)
    except UnexpectedExit as exc:
        raise ValueError(f"unable to read remote config: {remote_path}") from exc
    return result.stdout


def query_leaf_mounts(connection: HostConnection, roots: list[str]) -> list[str]:
    if offline_mode():
        raise ValueError("offline mode cannot query remote ZFS datasets")

    result = connection.connection.run("zfs list -H -o mountpoint", hide=True)
    mountpoints = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("/")]
    relevant = sorted(
        {
            mountpoint
            for mountpoint in mountpoints
            if any(mountpoint == root or mountpoint.startswith(f"{root}/") for root in roots)
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
    leaf_mounts: list[str],
) -> str:
    managed_slots = {f"mp{mount['slot']}:" for mount in root_mounts}
    root_mount_lines = [
        f"mp{mount['slot']}: {mount['source']},mp={mount['target']},backup={mount['backup']}"
        for mount in root_mounts
    ]
    idmapped_lines = [
        f"lxc.mount.entry: {mountpoint} mnt/{mountpoint.lstrip('/')} none bind,create=dir,idmap=container 0 0"
        for mountpoint in leaf_mounts
    ]

    rendered_lines: list[str] = []
    root_mounts_inserted = False
    idmapped_inserted = False
    for line in current_config.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(slot) for slot in managed_slots):
            if not root_mounts_inserted:
                rendered_lines.extend(root_mount_lines)
                root_mounts_inserted = True
            continue
        if stripped.startswith("lxc.mount.entry: "):
            source = stripped.split()[1]
            if any(source == root or source.startswith(f"{root}/") for root in idmapped_roots):
                if not idmapped_inserted:
                    rendered_lines.extend(idmapped_lines)
                    idmapped_inserted = True
                continue
        rendered_lines.append(line)

    if not root_mounts_inserted:
        rendered_lines.extend(root_mount_lines)
    if not idmapped_inserted:
        rendered_lines.extend(idmapped_lines)

    while rendered_lines and not rendered_lines[-1].strip():
        rendered_lines.pop()

    return "\n".join([*rendered_lines, ""])
