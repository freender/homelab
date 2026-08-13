from __future__ import annotations

import shutil
from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..output import print_action, print_sub, print_warn
from ..ssh import HostConnection, diff_many
from ..templates import render_template

REMOTE_ROOT = "/tmp/homelab-docker-stacks"
DEFAULT_APPDATA_ROOT = "/mnt/cache/appdata"
COMPOSE_NAME = "compose.yml"
TEMPLATE_NAME = "compose.yml.j2"


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="docker-stacks")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    if not hosts:
        print_action(f"Skipping docker-stacks (not applicable to {requested_host})")
        return 0

    validate(root, hosts)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1


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
    Downstream (diff, staging, install.sh) sees a single uniform directory and
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


def present_stacks(root: Path, host: str) -> list[str]:
    """Stacks the tree can actually produce a compose file for, on this host."""
    present = []
    for stack in all_stacks(root):
        base = stack_dir(root, stack)
        if (base / f"{host}.yml").is_file() or (base / TEMPLATE_NAME).is_file():
            present.append(stack)
    return present


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


def check_stack_tree(root: Path) -> None:
    """Structural rules for stacks/, independent of any one host.

    One directory per application. Inside it, either a single compose.yml.j2 for
    every host, or one <host>.yml per host that runs it -- never a mix, because a
    mix means some hosts silently follow the template and others do not.
    """
    registry = default_registry(root)
    known = set(registry.list_hosts(feature="docker-stacks"))

    for stack in all_stacks(root):
        base = stack_dir(root, stack)
        files = sorted(entry.name for entry in base.iterdir() if entry.is_file())
        if not files:
            raise ValueError(f"empty stack directory: {base}")

        has_template = TEMPLATE_NAME in files
        host_files = [name for name in files if name.endswith(".yml") and name != TEMPLATE_NAME]

        if has_template and host_files:
            raise ValueError(
                f"{base} mixes {TEMPLATE_NAME} with per-host file(s) "
                f"{', '.join(host_files)}; a stack is one or the other"
            )

        for name in files:
            if name == TEMPLATE_NAME:
                continue
            host = name[:-4] if name.endswith(".yml") else None
            if host not in known:
                raise ValueError(
                    f"{base / name}: expected {TEMPLATE_NAME} or <host>.yml for a host "
                    f"with the docker-stacks feature ({', '.join(sorted(known))})"
                )


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


def validate(root: Path, hosts: list[str]) -> None:
    check_stack_tree(root)
    for host in hosts:
        check_placement(root, host)
    check_shared_orphans(root)


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    ssh_user = str(registry.get(host, "config.user"))
    ssh_hostname = str(registry.get(host, "config.hostname", host))
    appdata_root = str(
        registry.get(host, "docker-stacks.appdata_root", DEFAULT_APPDATA_ROOT)
    ).strip()
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
        "scripts/install.sh",
        host,
        env=force_env(force),
        require_root=True,
        interpreter="bash",
        remote_subdirs=("build", "lib"),
    )
