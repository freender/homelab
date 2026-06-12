from __future__ import annotations

from pathlib import Path

from ..build import copy_file, copy_files, render_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import FileSpec, write_file_map
from ..output import print_action, print_error, print_sub
from ..ssh import HostConnection, build_files, diff_many

REMOTE_ROOT = "/tmp/homelab-pve-postinstall"
NOTIFY_SCRIPT = "notify-failure.sh"
NOTIFY_SERVICE = "homelab-notify-failure@.service"
PIKVM_ROUTES_SCRIPT = "homelab-cinci-pikvm-routes"
PIKVM_ROUTES_SERVICE = "homelab-cinci-pikvm-routes.service"
PVE_FILES = [
    "proxmox.sources",
    "ceph.sources",
    "pve-test.sources",
    "no-nag-script",
    "pve-remove-nag.sh",
    "sshd-hardening.conf",
    NOTIFY_SCRIPT,
    NOTIFY_SERVICE,
    "homelab-pve-cluster-rejoin-helper",
    PIKVM_ROUTES_SCRIPT,
    PIKVM_ROUTES_SERVICE,
]
GENERATED_FILES = {
    NOTIFY_SCRIPT,
    NOTIFY_SERVICE,
    "homelab-pve-cluster-rejoin-helper",
    PIKVM_ROUTES_SCRIPT,
    PIKVM_ROUTES_SERVICE,
}

REMOTE_PATHS = {
    "proxmox.sources": "/etc/apt/sources.list.d/proxmox.sources",
    "ceph.sources": "/etc/apt/sources.list.d/ceph.sources",
    "pve-test.sources": "/etc/apt/sources.list.d/pve-test.sources",
    "no-nag-script": "/etc/apt/apt.conf.d/no-nag-script",
    "pve-remove-nag.sh": "/usr/local/bin/pve-remove-nag.sh",
    "sshd-hardening.conf": "/etc/ssh/sshd_config.d/99-disable-password-auth.conf",
    NOTIFY_SCRIPT: "/usr/local/bin/homelab-notify-failure",
    NOTIFY_SERVICE: "/etc/systemd/system/homelab-notify-failure@.service",
    "homelab-pve-cluster-rejoin-helper": "/usr/local/sbin/homelab-pve-cluster-rejoin-helper",
    PIKVM_ROUTES_SCRIPT: "/usr/local/sbin/homelab-cinci-pikvm-routes",
    PIKVM_ROUTES_SERVICE: "/etc/systemd/system/homelab-cinci-pikvm-routes.service",
}

MODES = {
    "pve-remove-nag.sh": "755",
    NOTIFY_SCRIPT: "755",
    "homelab-pve-cluster-rejoin-helper": "755",
    PIKVM_ROUTES_SCRIPT: "755",
}
FILE_SPECS = tuple(
    FileSpec(file_name, REMOTE_PATHS[file_name], MODES.get(file_name, "644"))
    for file_name in PVE_FILES
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="pve-postinstall")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping pve-postinstall (not applicable to {requested_host})")
        return 0

    try:
        validate(root)
    except ValueError as exc:
        print_error(str(exc))
        return 1

    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


def validate(root: Path) -> None:
    config_dir = root / "pve-postinstall" / "configs" / "pve"
    interfaces_template = root / "pve-postinstall" / "templates" / "pve-interfaces"
    notify_script = root / "shared" / "scripts" / NOTIFY_SCRIPT
    notify_template = root / "shared" / "templates" / NOTIFY_SERVICE

    for file_name in PVE_FILES:
        if file_name in GENERATED_FILES:
            continue
        file_path = config_dir / file_name
        if not file_path.is_file():
            raise ValueError(f"missing config file: {file_path}")

    if not interfaces_template.is_file():
        raise ValueError(f"missing interfaces template: {interfaces_template}")
    if not notify_script.is_file():
        raise ValueError(f"missing notify script: {notify_script}")
    if not notify_template.is_file():
        raise ValueError(f"missing notify template: {notify_template}")


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        host_type = registry.get(host, "config.type")
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    timezone = str(registry.get(host, "pve-postinstall.timezone", "UTC"))
    import_pools_raw = registry.get(host, "pve-postinstall.import_pools", [])
    if not isinstance(import_pools_raw, list):
        raise ValueError(f"pve-postinstall.import_pools must be a list for {host}")
    import_pools = " ".join(str(p) for p in import_pools_raw)

    mounts: list[str] = []
    mounts_raw = registry.get(host, "pve-postinstall.mounts", None)
    if mounts_raw is None:
        mounts_raw = []
    if not isinstance(mounts_raw, list):
        raise ValueError(f"pve-postinstall.mounts must be a list for {host}")
    for m in mounts_raw:
        if not isinstance(m, dict) or "label" not in m or "path" not in m:
            raise ValueError(f"pve-postinstall.mounts entry must have label and path for {host}")
        mounts.append(f"{m['label']}:{m['path']}")
    mounts_str = " ".join(mounts)
    expected_clustered = str(
        host_type == "pve" and not bool(registry.get(host, "config.standalone", False))
    ).lower()
    cluster_link0 = ""
    if expected_clustered == "true":
        mgmt_ip = str(registry.get(host, "pve-postinstall.interfaces.mgmt_ip", ""))
        cluster_link0 = (
            mgmt_ip.split("/", 1)[0]
            if mgmt_ip
            else str(registry.get(host, "config.hostname"))
        )

    if host_type != "pve":
        raise ValueError(f"Unsupported host type for {host}: {host_type}")

    module_dir = root / "pve-postinstall"
    config_dir = module_dir / "configs" / "pve"
    build_dir = module_dir / "build" / host
    prepare_build_dir(build_dir)

    for file_name in PVE_FILES:
        if file_name in GENERATED_FILES:
            continue
        source_path = config_dir / file_name
        if not source_path.is_file():
            raise ValueError(f"Missing config file: {source_path}")
    copy_files(
        config_dir,
        build_dir,
        [
            file_name
            for file_name in PVE_FILES
            if file_name not in GENERATED_FILES
        ],
    )
    copy_file(
        root / "shared" / "scripts" / NOTIFY_SCRIPT,
        build_dir / NOTIFY_SCRIPT,
    )
    render_file(
        root / "shared" / "templates" / NOTIFY_SERVICE,
        build_dir / NOTIFY_SERVICE,
        NOTIFY_SCRIPT="/usr/local/bin/homelab-notify-failure",
    )
    build_cluster_rejoin_helper(root, build_dir)
    build_pikvm_routes(root, host, build_dir)

    write_file_map(build_dir, FILE_SPECS)
    build_network_interfaces_bundle(root, host, build_dir)

    connection = HostConnection(host)
    print_sub("Comparing with remote configs...")
    for message in diff_many(
        connection,
        [(build_dir / file_name, REMOTE_PATHS[file_name]) for file_name in PVE_FILES],
    ):
        print_sub(message)

    interfaces_path = build_dir / "interfaces"
    if interfaces_path.is_file():
        _, message = connection.remote_diff(interfaces_path, "/etc/network/interfaces")
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        if interfaces_path.is_file():
            print_sub("Network interfaces subfeature: enabled")
        else:
            print_sub("Network interfaces subfeature: disabled")
        if (build_dir / PIKVM_ROUTES_SCRIPT).is_file():
            print_sub("Pi-KVM routes subfeature: enabled")
        else:
            print_sub("Pi-KVM routes subfeature: disabled")
        return

    stage_and_install(
        root,
        host,
        host_type,
        timezone,
        import_pools,
        mounts_str,
        expected_clustered,
        cluster_link0,
        build_dir,
        connection,
        force=force,
    )

def build_network_interfaces_bundle(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    try:
        interfaces_config = registry.get(host, "pve-postinstall.interfaces")
    except HostLookupError:
        return

    if not isinstance(interfaces_config, dict):
        return

    try:
        mgmt_ip = str(registry.get(host, "pve-postinstall.interfaces.mgmt_ip"))
        gateway = str(registry.get(host, "pve-postinstall.interfaces.gateway"))
        storage_ip = str(registry.get(host, "pve-postinstall.interfaces.storage_ip"))
        mgmt_iface = str(registry.get(host, "pve-postinstall.interfaces.mgmt_iface", "nic0"))
        storage_iface = str(registry.get(host, "pve-postinstall.interfaces.storage_iface", "nic1"))
    except HostLookupError as exc:
        raise ValueError(
            f"pve-postinstall.interfaces.{{mgmt_ip,gateway,storage_ip}} required for {host}"
        ) from exc

    render_file(
        root / "pve-postinstall" / "templates" / "pve-interfaces",
        build_dir / "interfaces",
        NET_MGMT_IP=mgmt_ip,
        NET_GATEWAY=gateway,
        NET_STORAGE_IP=storage_ip,
        NET_MGMT_IFACE=mgmt_iface,
        NET_STORAGE_IFACE=storage_iface,
    )


def build_pikvm_routes(root: Path, host: str, build_dir: Path) -> None:
    registry = default_registry(root)
    try:
        config = registry.get(host, "pve-postinstall.pikvm_routes")
    except HostLookupError:
        return

    if not isinstance(config, dict) or not bool(config.get("enabled", True)):
        return

    gateway = str(config.get("gateway", "")).strip()
    interface = str(config.get("interface", "vmbr0")).strip()
    subnets = config.get("subnets", [])
    host_records = config.get("host_records", [])
    if not gateway:
        raise ValueError(f"pve-postinstall.pikvm_routes.gateway required for {host}")
    if not interface:
        raise ValueError(f"pve-postinstall.pikvm_routes.interface required for {host}")
    if not isinstance(subnets, list) or not subnets:
        raise ValueError(
            f"pve-postinstall.pikvm_routes.subnets must be a non-empty list for {host}"
        )
    if not isinstance(host_records, list):
        raise ValueError(f"pve-postinstall.pikvm_routes.host_records must be a list for {host}")

    lines = [
        "#!/bin/sh",
        "set -eu",
        f"PIKVM_GW=${{PIKVM_GW:-{gateway}}}",
        f"LAN_IF=${{LAN_IF:-{interface}}}",
        "for subnet in \\",
    ]
    for subnet in subnets:
        lines.append(f"    {str(subnet).strip()} \\")
    lines.extend(
        [
            "    ; do",
            "    ip route replace \"$subnet\" via \"$PIKVM_GW\" dev \"$LAN_IF\"",
            "done",
        ]
    )
    for record in host_records:
        if not isinstance(record, dict):
            raise ValueError(f"invalid pikvm_routes.host_records entry for {host}")
        ip = str(record.get("ip", "")).strip()
        names_raw = record.get("names", [])
        if not ip or not isinstance(names_raw, list) or not names_raw:
            raise ValueError(f"pikvm_routes.host_records entries need ip and names for {host}")
        names = " ".join(str(name).strip() for name in names_raw if str(name).strip())
        first_name = names.split(" ", 1)[0]
        escaped_ip = ip.replace(".", r"\.")
        escaped_name = first_name.replace(".", r"\.")
        lines.extend(
            [
                "if ! grep -Eq "
                f"'^[[:space:]]*{escaped_ip}[[:space:]].*{escaped_name}' /etc/hosts; then",
                f"    printf '%s\\n' '{ip} {names}' >> /etc/hosts",
                "fi",
            ]
        )
    (build_dir / PIKVM_ROUTES_SCRIPT).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (build_dir / PIKVM_ROUTES_SERVICE).write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Cinci routes via relocated Pi-KVM WireGuard router",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=oneshot",
                "ExecStart=/usr/local/sbin/homelab-cinci-pikvm-routes",
                "RemainAfterExit=yes",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        ),
        encoding="utf-8",
    )


def build_cluster_rejoin_helper(root: Path, build_dir: Path) -> None:
    registry = default_registry(root)
    pve_hosts = registry.list_hosts(feature="pve-postinstall")
    lines = [
        "#!/bin/bash",
        "# Safe PVE rebuild helper. Does not execute pvecm add.",
        "set -euo pipefail",
        "",
        "usage() {",
        "    cat <<'EOF'",
        "Usage: homelab-pve-cluster-rejoin-helper <node> [cluster-peer]",
        "",
        "Runs safe cluster-side cleanup/readiness checks and prints the manual join command.",
        "Run this on an existing cluster node after rebuilding <node> but before manual pvecm add.",
        "",
        "This helper may run pvecm delnode and remove stale known_hosts entries.",
        "It never runs pvecm add.",
        "EOF",
        "}",
        "",
        "node=${1:-}",
        "cluster_peer=${2:-}",
        "if [[ -z \"$node\" || \"$node\" == \"-h\" || \"$node\" == \"--help\" ]]; then",
        "    usage",
        "    exit 0",
        "fi",
        "",
        "case \"$node\" in",
    ]
    for pve_host in pve_hosts:
        hostname = str(registry.get(pve_host, "config.hostname"))
        mgmt_ip = str(registry.get(pve_host, "pve-postinstall.interfaces.mgmt_ip", ""))
        link0 = mgmt_ip.split("/", 1)[0] if mgmt_ip else hostname
        standalone = bool(registry.get(pve_host, "config.standalone", False))
        lines.extend(
            [
                f"    {pve_host})",
                f"        node_fqdn='{hostname}'",
                f"        node_link0='{link0}'",
                f"        node_standalone='{str(standalone).lower()}'",
                "        ;;",
            ]
        )
    lines.extend(
        [
            "    *)",
            "        echo \"Unknown PVE node: $node\" >&2",
            f"        echo \"Known nodes: {' '.join(pve_hosts)}\" >&2",
            "        exit 1",
            "        ;;",
            "esac",
            "",
            "if [[ \"$node_standalone\" == \"true\" ]]; then",
            "    echo \"$node is marked standalone; cluster join is not expected.\" >&2",
            "    exit 1",
            "fi",
            "",
            "local_node=$(hostname -s)",
            "if [[ \"$node\" == \"$local_node\" ]]; then",
            "    echo \"Refusing to clean up local node $node from itself.\" >&2",
            "    echo \"Run this on another cluster node.\" >&2",
            "    exit 1",
            "fi",
            "",
            "if [[ -z \"$cluster_peer\" ]]; then",
            "    cluster_peer=$local_node.freender.internal",
            "fi",
            "",
            "echo \"==> Cleaning stale cluster state for $node\"",
            "if pvecm nodes 2>/dev/null | awk '{print $3}' | grep -Fxq \"$node\"; then",
            "    pvecm delnode \"$node\"",
            "else",
            "    echo \"$node is not listed in pvecm nodes; skipping pvecm delnode\"",
            "fi",
            "",
            "if [[ -d \"/etc/pve/nodes/$node\" ]]; then",
            "    rm -rf \"/etc/pve/nodes/$node\"",
            "    echo \"Removed /etc/pve/nodes/$node\"",
            "else",
            "    echo \"/etc/pve/nodes/$node absent; skipping\"",
            "fi",
            "",
            "echo \"==> Removing stale SSH known_hosts entries\"",
            "for host in \"$node\" \"$node_fqdn\" \"$node_link0\"; do",
            "    ssh-keygen -R \"$host\" >/dev/null 2>&1 || true",
            "done",
            "",
            "echo \"==> Waiting for rebuilt node readiness\"",
            "for attempt in $(seq 1 60); do",
            "    if timeout 3 bash -c \"</dev/tcp/$node_link0/22\" >/dev/null 2>&1; then",
            "        echo \"SSH is reachable on $node_link0\"",
            "        break",
            "    fi",
            "    if [[ \"$attempt\" -eq 60 ]]; then",
            "        echo \"SSH did not become reachable on $node_link0\" >&2",
            "        exit 1",
            "    fi",
            "    sleep 10",
            "done",
            "",
            "if timeout 3 bash -c \"</dev/tcp/$node_link0/8006\" >/dev/null 2>&1; then",
            "    echo \"PVE API port is reachable on $node_link0\"",
            "else",
            "    echo \"PVE API port 8006 is not reachable yet on $node_link0\" >&2",
            "fi",
            "",
            "fingerprint=",
            "if command -v openssl >/dev/null 2>&1; then",
            "    fingerprint=$(openssl s_client -connect \"$cluster_peer:8006\" \\",
            "        -servername \"$cluster_peer\" </dev/null 2>/dev/null \\",
            "        | openssl x509 -noout -fingerprint -sha256 2>/dev/null \\",
            "        | cut -d= -f2 || true)",
            "fi",
            "",
            "echo \"==> Manual join command\"",
            "echo \"Run on rebuilt node $node after confirming hostname/IP are correct:\"",
            "if [[ -n \"$fingerprint\" ]]; then",
            "    echo \"pvecm add $cluster_peer --fingerprint $fingerprint --link0 $node_link0\"",
            "else",
            "    echo \"pvecm add $cluster_peer --link0 $node_link0\"",
            "    echo \"Fingerprint lookup failed; verify it manually.\" >&2",
            "fi",
        ]
    )
    (build_dir / "homelab-pve-cluster-rejoin-helper").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def stage_and_install(
    root: Path,
    host: str,
    host_type: str,
    timezone: str,
    import_pools: str,
    mounts: str,
    expected_clustered: str,
    cluster_link0: str,
    build_dir: Path,
    connection: HostConnection,
    force: bool,
) -> None:
    upload_paths = [
        (build_dir, f"{REMOTE_ROOT}/build/{host}"),
        (root / "pve-postinstall" / "scripts", f"{REMOTE_ROOT}/scripts"),
    ]
    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        upload_paths,
        "scripts/install.sh",
        host,
        host_type,
        timezone,
        import_pools,
        mounts,
        expected_clustered,
        cluster_link0,
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib"),
    )
