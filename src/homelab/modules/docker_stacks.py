from __future__ import annotations

import posixpath
import shutil
from pathlib import Path

import yaml

from .. import backup_excludes
from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import is_qemu_guest, run_module_deploy
from ..output import print_sub, print_warn
from ..ssh import HostConnection, diff_many
from ..templates import render_template, render_template_text

REMOTE_ROOT = "/tmp/homelab-docker-stacks"
INSTALLER = "scripts/install.py"
INTERPRETER = "python3"
DEFAULT_APPDATA_ROOT = "/mnt/cache/appdata"
COMPOSE_NAME = "compose.yml"
TEMPLATE_NAME = "compose.yml.j2"
APPDATA_EXCLUDE_PROFILE = "appdata"

# (host, stack, service, bind source)
ScratchViolation = tuple[str, str, str, str]

# helm's mounts from before check_vm_scratch_placement existed. May only shrink;
# homelab-ops#158 moves them to /mnt/scratch and deletes the list. victoria-logs
# is already on the backup=0 disk through a hand-made bind mount, but its compose
# path is what the check reads.
KNOWN_SCRATCH_VIOLATIONS: frozenset[ScratchViolation] = frozenset(
    {
        ("helm", "alloy", "alloy-helm", "/mnt/cache/appdata/alloy/data"),
        ("helm", "alloy", "alloy-helm", "/mnt/cache/appdata/geoipupdate"),
        ("helm", "alloy", "alloy-helm", "/mnt/cache/appdata/traefik/logs"),
        ("helm", "geoipupdate", "geoipupdate-helm", "/mnt/cache/appdata/geoipupdate"),
        ("helm", "traefik", "crowdsec-helm", "/mnt/cache/appdata/traefik/logs"),
        ("helm", "traefik", "traefik-helm", "/mnt/cache/appdata/traefik/logs"),
        ("helm", "traefik", "traefik-helm", "/mnt/cache/appdata/traefik/plugins-storage"),
        ("helm", "victoria-logs", "victoria-logs", "/mnt/cache/appdata/victoria-logs/data"),
        ("helm", "vmagent", "vmagent-helm", "/mnt/cache/appdata/vmagent/data"),
    }
)


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
        "docker-stacks",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported_hosts, hosts: validate(root, hosts),
    )


def stacks_root(root: Path) -> Path:
    return root / "docker-stacks" / "stacks"


def stack_dir(root: Path, stack: str) -> Path:
    return stacks_root(root) / stack


def all_stacks(root: Path) -> list[str]:
    """Every stack directory in the repo, regardless of how it is defined.

    One directory per application, named for the application. Which hosts run it
    is hosts.conf's business, not the tree's.
    """
    base = stacks_root(root)
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir() if entry.is_dir())


def shared_stacks(root: Path) -> list[str]:
    """Stacks defined once (compose.yml.j2) and rendered for every host.

    A stack qualifies only when every host running it wants byte-identical config
    apart from its own name. Anything needing a per-host difference gets
    <host>.yml files instead -- see the README.
    """
    return sorted(
        stack for stack in all_stacks(root) if (stack_dir(root, stack) / TEMPLATE_NAME).is_file()
    )


def resolve_stack(root: Path, host: str, stack: str) -> tuple[str, Path]:
    """Locate one declared stack for one host, refusing anything ambiguous.

    Returns ("shared", template) or ("host", per-host file). A stack carrying
    both forms is rejected rather than resolved by precedence: a silent winner is
    exactly how someone edits the template and watches one host ignore it.
    """
    base = stack_dir(root, stack)
    host_path = base / f"{host}.yml"
    template_path = base / TEMPLATE_NAME

    if host_path.is_file() and template_path.is_file():
        raise ValueError(
            f"{host}: stack '{stack}' has both {host_path.name} and {TEMPLATE_NAME} "
            f"in {base}; delete one"
        )
    if host_path.is_file():
        return "host", host_path
    if template_path.is_file():
        return "shared", template_path
    raise ValueError(
        f"{host}: hosts.conf declares stack '{stack}' but {base} has neither "
        f"{host}.yml nor {TEMPLATE_NAME}"
    )


def assemble_stacks(root: Path, host: str, stacks: list[str], out_dir: Path) -> dict[str, str]:
    """Materialize every declared stack for a host into one staging tree.

    Shared templates are rendered with HOST; per-host files are copied verbatim.
    Downstream (diff, staging, install.py) sees a single uniform directory and
    does not care which source a stack came from.
    """
    origins: dict[str, str] = {}
    for stack in stacks:
        origin, source = resolve_stack(root, host, stack)
        destination = out_dir / stack / COMPOSE_NAME
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin == "shared":
            render_template(source, destination, HOST=host)
        else:
            shutil.copyfile(source, destination)
        origins[stack] = origin
    return origins


def declared_stacks(root: Path, host: str) -> list[str]:
    """Stacks hosts.conf says this host runs.

    hosts.conf is the canonical answer to "which host runs <app>"; the directory
    tree is the payload for that answer, not the source of it.
    """
    declared = default_registry(root).get(host, "docker-stacks.stacks", [])
    if not isinstance(declared, list):
        raise ValueError(
            f"docker-stacks.stacks for {host} must be a list, got {type(declared).__name__}"
        )
    return sorted(str(stack) for stack in declared)


def host_stacks(root: Path, host: str) -> list[str]:
    """Deployable stacks for a host: the hosts.conf declaration, once verified."""
    check_placement(root, host)
    return declared_stacks(root, host)


def check_placement(root: Path, host: str) -> None:
    """Fail on any disagreement between hosts.conf and the stack tree.

    Drift is checked in both directions on purpose. A declared stack with no
    compose file would deploy nothing while inventory claims otherwise; an
    undeclared directory would make a `git mv` silently move a service between
    hosts without an inventory change. Neither may pass quietly.
    """
    declared = set(declared_stacks(root, host))

    if not declared:
        raise ValueError(f"{host} enables docker-stacks but declares no stacks in hosts.conf")

    # resolve_stack owns "declared but missing" and "defined twice", so every
    # declared name is proven to have exactly one definition.
    for stack in sorted(declared):
        resolve_stack(root, host, stack)

    # A <host>.yml exists for a stack this host does not declare. Renaming a file
    # between hosts must not relocate a service without an inventory change.
    undeclared = sorted(
        stack
        for stack in all_stacks(root)
        if stack not in declared and (stack_dir(root, stack) / f"{host}.yml").is_file()
    )
    if undeclared:
        raise ValueError(
            f"{host}: {host}.yml present for stack(s) not declared in hosts.conf: "
            f"{', '.join(undeclared)}"
        )


def _check_no_definition_mix(base: Path, files: list[str]) -> None:
    """A stack is defined one way or the other, never both.

    A mix means some hosts silently follow the template and others do not, so
    editing the template would quietly skip whichever hosts carry their own file.
    """
    host_files = [name for name in files if name.endswith(".yml") and name != TEMPLATE_NAME]
    if TEMPLATE_NAME in files and host_files:
        raise ValueError(
            f"{base} mixes {TEMPLATE_NAME} with per-host file(s) "
            f"{', '.join(host_files)}; a stack is one or the other"
        )


def _check_filenames_name_known_hosts(base: Path, files: list[str], known: set[str]) -> None:
    """Every non-template file must be `<host>.yml` for a host that runs stacks.

    `towerr.yml` sitting in the tree deploying to nothing is the failure here.
    """
    for name in files:
        if name == TEMPLATE_NAME:
            continue
        host = name[:-4] if name.endswith(".yml") else None
        if host not in known:
            raise ValueError(
                f"{base / name}: expected {TEMPLATE_NAME} or <host>.yml for a host "
                f"with the docker-stacks feature ({', '.join(sorted(known))})"
            )


def _check_stack_dir(base: Path, known: set[str]) -> None:
    """Structural rules for one stack directory."""
    files = sorted(entry.name for entry in base.iterdir() if entry.is_file())
    if not files:
        raise ValueError(f"empty stack directory: {base}")
    _check_no_definition_mix(base, files)
    _check_filenames_name_known_hosts(base, files, known)


def check_stack_tree(root: Path) -> None:
    """Structural rules for stacks/, independent of any one host.

    One directory per application. Inside it, either a single compose.yml.j2 for
    every host, or one <host>.yml per host that runs it -- never a mix, because a
    mix means some hosts silently follow the template and others do not.
    """
    registry = default_registry(root)
    known = set(registry.list_hosts(feature="docker-stacks"))

    for stack in all_stacks(root):
        _check_stack_dir(stack_dir(root, stack), known)


def check_shared_orphans(root: Path) -> None:
    """A stack no host declares is dead code; fail rather than carry it."""
    registry = default_registry(root)
    declared: set[str] = set()
    for host in registry.list_hosts(feature="docker-stacks"):
        declared.update(declared_stacks(root, host))

    orphans = sorted(set(all_stacks(root)) - declared)
    if orphans:
        raise ValueError(
            f"stack director(ies) declared by no host in hosts.conf: {', '.join(orphans)}"
        )


def appdata_root_for(root: Path, host: str) -> str:
    registry = default_registry(root)
    return str(registry.get(host, "docker-stacks.appdata_root", DEFAULT_APPDATA_ROOT)).strip()


def is_vm(root: Path, host: str) -> bool:
    return is_qemu_guest(default_registry(root), host)


def compose_document(root: Path, host: str, stack: str) -> dict:
    """The compose file one host would deploy for one stack, parsed."""
    origin, source = resolve_stack(root, host, stack)
    if origin == "shared":
        text = render_template_text(source, HOST=host)
    else:
        text = source.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    return document if isinstance(document, dict) else {}


def bind_sources(service: object) -> list[str]:
    """Host-side paths of a service's bind mounts, normalized; named volumes skipped."""
    if not isinstance(service, dict):
        return []
    sources = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, str):
            source = volume.split(":", 1)[0]
        elif isinstance(volume, dict) and volume.get("type") == "bind":
            source = str(volume.get("source", ""))
        else:
            continue
        if source.startswith("/"):
            sources.append(posixpath.normpath(source))
    return sources


def appdata_relative(source: str, appdata_root: str) -> str | None:
    """`source` relative to the appdata root (`/grafana`), or None when outside it."""
    base = posixpath.normpath(appdata_root)
    if source == base:
        return "/"
    if source.startswith(base + "/"):
        return source[len(base) :]
    return None


def scratch_violations(
    root: Path, host: str, stacks: list[str], entries: list[str]
) -> set[ScratchViolation]:
    """Bind mounts under the appdata root whose path an appdata exclude covers."""
    appdata_root = appdata_root_for(root, host)
    found: set[ScratchViolation] = set()
    for stack in stacks:
        services = compose_document(root, host, stack).get("services") or {}
        for name, service in services.items():
            for source in bind_sources(service):
                relative = appdata_relative(source, appdata_root)
                if relative is not None and backup_excludes.matching_entry(relative, entries):
                    found.add((host, stack, str(name), source))
    return found


def _describe(violations: set[ScratchViolation]) -> str:
    return "; ".join(
        f"{host} {stack}/{service}: {source}"
        for host, stack, service, source in sorted(violations)
    )


def check_vm_scratch_placement(
    root: Path,
    hosts: list[str],
    known: frozenset[ScratchViolation] = KNOWN_SCRATCH_VIOLATIONS,
) -> None:
    """On a VM, regenerable data belongs on the scratch disk, not under appdata.

    `exclude-path` is file-level and does nothing for a VM's block backup, so a
    path `appdata.txt` excludes on an LXC is backed up in full on a VM unless it
    lives on the backup=0 scratch disk (homelab-ops#154). Fails on a violation
    not in `known`, and on a `known` entry that no longer violates, so the list
    only shrinks.
    """
    vm_hosts = [host for host in hosts if is_vm(root, host)]
    if not vm_hosts:
        return
    entries = backup_excludes.load_profile(root, APPDATA_EXCLUDE_PROFILE)
    found: set[ScratchViolation] = set()
    for host in vm_hosts:
        found |= scratch_violations(root, host, declared_stacks(root, host), entries)

    new = found - known
    if new:
        raise ValueError(
            f"VM stack volume(s) under appdata match {APPDATA_EXCLUDE_PROFILE}.txt, so a "
            f"VM backup would include them: {_describe(new)}. "
            "Mount them from /mnt/scratch/<stack>."
        )
    fixed = {entry for entry in known - found if entry[0] in vm_hosts}
    if fixed:
        raise ValueError(
            f"KNOWN_SCRATCH_VIOLATIONS lists mount(s) that no longer violate: "
            f"{_describe(fixed)}. Remove them from the list."
        )
    pending = len(known & found)
    if pending:
        print_warn(f"{pending} known VM appdata-exclude mount(s) pending homelab-ops#158")


def validate(root: Path, hosts: list[str]) -> None:
    check_stack_tree(root)
    for host in hosts:
        check_placement(root, host)
    check_shared_orphans(root)
    check_vm_scratch_placement(root, hosts)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_user = str(registry.get(host, "config.user"))
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    appdata_root = appdata_root_for(root, host)
    apply_changed = registry.get(host, "docker-stacks.apply_changed", True)
    apply_changed_flag = "true" if apply_changed else "false"

    stacks = host_stacks(root, host)

    build_dir = root / "docker-stacks" / "build" / host
    prepare_build_dir(build_dir)
    staged = build_dir / "stacks"
    origins = assemble_stacks(root, host, stacks, staged)
    shared_count = sum(1 for origin in origins.values() if origin == "shared")

    write_env_file(
        build_dir / "env",
        {
            "APPDATA_ROOT": appdata_root,
            "APPLY_CHANGED": apply_changed_flag,
            "MANAGED_STACK_COUNT": str(len(stacks)),
        },
    )

    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)
    print_sub(
        f"Comparing {len(stacks)} stack(s) with remote "
        f"({shared_count} shared, {len(stacks) - shared_count} host-specific)..."
    )
    diff_pairs = [
        (staged / stack / COMPOSE_NAME, f"{appdata_root}/{stack}/{COMPOSE_NAME}")
        for stack in stacks
    ]
    for message in diff_many(connection, diff_pairs):
        print_sub(message)

    if apply_changed_flag != "true":
        print_warn(
            f"{host}: apply_changed is false; compose files sync but containers are not reconciled"
        )

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy to {host}:{REMOTE_ROOT}/")
        print_sub(f"[DRY-RUN] Appdata root: {appdata_root}")
        print_sub("Managed stacks:")
        for stack in stacks:
            print_sub(f"    {stack} ({origins[stack]})")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / "docker-stacks" / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        INSTALLER,
        host,
        env=force_env(force),
        require_root=True,
        interpreter=INTERPRETER,
        remote_subdirs=("build", "src"),
    )
