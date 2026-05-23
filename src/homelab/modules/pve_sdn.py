from __future__ import annotations

from pathlib import Path

from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_sub
from ..ssh import HostConnection, build_files

REMOTE_ROOT = "/tmp/homelab-pve-sdn"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-sdn")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-sdn (not applicable to {requested_host})")
        return 0

    validate(root, supported_hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path, hosts: list[str]) -> None:
    script = root / "pve-sdn" / "scripts" / "install.sh"
    if not script.is_file():
        raise ValueError(f"missing installer: {script}")
    registry = default_registry(root)
    for host in hosts:
        normalize_plan(registry, host)


def shell_quote(value: object) -> str:
    return str(value).replace("'", "'\"'\"'")


def normalize_plan(registry, host: str) -> dict[str, object]:
    plan = registry.get(host, "pve-sdn")
    if not isinstance(plan, dict):
        raise ValueError(f"pve-sdn must be a mapping for {host}")
    zone = str(plan.get("zone", "vlans")).strip()
    bridge = str(plan.get("bridge", "vmbr0")).strip()
    nodes = plan.get("nodes", [host])
    vnets = plan.get("vnets", [])
    if not zone or not bridge:
        raise ValueError(f"pve-sdn zone and bridge are required for {host}")
    if not isinstance(nodes, list) or not all(str(node).strip() for node in nodes):
        raise ValueError(f"pve-sdn.nodes must be a non-empty list for {host}")
    if not isinstance(vnets, list) or not vnets:
        raise ValueError(f"pve-sdn.vnets must be a non-empty list for {host}")
    normalized_vnets = []
    for index, vnet in enumerate(vnets):
        if not isinstance(vnet, dict):
            raise ValueError(f"pve-sdn.vnets[{index}] must be a mapping for {host}")
        name = str(vnet.get("name", "")).strip()
        tag = str(vnet.get("tag", "")).strip()
        alias = str(vnet.get("alias", "")).strip()
        if not name or not tag:
            raise ValueError(f"pve-sdn.vnets[{index}] requires name and tag for {host}")
        normalized_vnets.append({"name": name, "tag": tag, "alias": alias})
    return {
        "zone": zone,
        "bridge": bridge,
        "nodes": [str(node).strip() for node in nodes],
        "vnets": normalized_vnets,
    }


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    if str(registry.get(host, "config.type")) != "pve":
        raise ValueError(f"Unsupported host type for {host}: {registry.get(host, 'config.type')}")

    build_dir = root / "pve-sdn" / "build" / host
    prepare_build_dir(build_dir)
    plan = normalize_plan(registry, host)
    write_plan(build_dir, plan)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        HostConnection(host),
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "pve-sdn" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )


def write_plan(build_dir: Path, plan: dict[str, object]) -> None:
    vnets = plan["vnets"]
    assert isinstance(vnets, list)
    lines = [
        f"ZONE='{shell_quote(plan['zone'])}'",
        f"BRIDGE='{shell_quote(plan['bridge'])}'",
        f"NODES='{shell_quote(','.join(plan['nodes']))}'",
        f"VNET_COUNT='{len(vnets)}'",
    ]
    for index, vnet in enumerate(vnets):
        lines.extend([
            f"VNET_{index}_NAME='{shell_quote(vnet['name'])}'",
            f"VNET_{index}_TAG='{shell_quote(vnet['tag'])}'",
            f"VNET_{index}_ALIAS='{shell_quote(vnet['alias'])}'",
        ])
    (build_dir / "sdn-plan.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")
