"""guest-baseline: the login and locale baseline of a Debian guest (homelab-ops#162).

Creates `freender` when missing, installs its sudoers file (sudo asks for a
password) and the key-only sshd drop-in, and sets the timezone and locale. The
values are the fleet's, not per host, so `guest-baseline: true` is the whole
config. It is the guest counterpart of `ubuntu-setup`, which stays the
bare-metal module; a host may have one or the other, never both, because both
own sshd and sudoers.

The installer owns every decision that needs live state. A dry run stages it
with `PLAN_ONLY=true`, so it prints what it would change without writing.
"""

from __future__ import annotations

from pathlib import Path

from ..build import copy_files, write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, connection_for_host, run_module_deploy, write_file_map
from ..op_secrets import offline_mode
from ..output import print_sub

FEATURE = "guest-baseline"
CONFLICTING_FEATURE = "ubuntu-setup"
REMOTE_ROOT = "/tmp/homelab-guest-baseline"
INSTALLER = "scripts/install.py"
# The fleet's Debian-family type; zion is `haos` and cannot run this.
SUPPORTED_TYPE = "ubuntu"

USER = "freender"
# New VMs boot with cloud-init `users: []` (homelab-ops#153), so 1000 is free.
USER_ID = "1000"
TIMEZONE = "America/New_York"
LOCALE = "en_US.UTF-8"

SSHD_DROP_IN = "99-homelab-key-only.conf"
# Hand-placed on helm, tower, riven and xur before this module; same settings.
RETIRED_SSHD_DROP_INS = ("/etc/ssh/sshd_config.d/99-disable-password-auth.conf",)

FILE_SPECS = (
    FileSpec("sudoers", f"/etc/sudoers.d/{USER}", mode="440"),
    FileSpec(SSHD_DROP_IN, f"/etc/ssh/sshd_config.d/{SSHD_DROP_IN}"),
)
# Only read when the user is created; an existing account's keys are its own.
AUTHORIZED_KEYS = "authorized_keys"
STATIC_FILES = (*(spec.build_name for spec in FILE_SPECS), AUTHORIZED_KEYS)


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
        FEATURE,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda supported_hosts, _hosts: validate(root, supported_hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    module_dir = root / FEATURE
    for path in (module_dir / INSTALLER, *(module_dir / "configs" / name for name in STATIC_FILES)):
        if not path.is_file():
            raise ValueError(f"missing required file: {path}")
    registry = default_registry(root)
    for host in hosts:
        host_type = str(registry.get(host, "config.type"))
        if host_type != SUPPORTED_TYPE:
            raise ValueError(f"{host}: {FEATURE} only supports {SUPPORTED_TYPE} hosts")
        if registry.has(host, CONFLICTING_FEATURE):
            raise ValueError(
                f"{host}: {FEATURE} and {CONFLICTING_FEATURE} both own sshd and sudoers; "
                "enable one"
            )


def build_host(root: Path, host: str) -> Path:
    build_dir = root / FEATURE / "build" / host
    prepare_build_dir(build_dir)
    copy_files(root / FEATURE / "configs", build_dir, list(STATIC_FILES))
    write_file_map(build_dir, FILE_SPECS)
    write_env_file(
        build_dir / "env",
        {
            "USER_NAME": USER,
            "USER_ID": USER_ID,
            "TIMEZONE": TIMEZONE,
            "LOCALE": LOCALE,
            "RETIRED_SSHD_DROP_INS": " ".join(RETIRED_SSHD_DROP_INS),
        },
    )
    return build_dir


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    build_dir = build_host(root, host)
    if dry_run and offline_mode():
        print_sub("[?] live baseline plan skipped (offline validation)")
        return
    stage_and_run_remote_installer(
        root,
        connection_for_host(root, host),
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / FEATURE / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        INSTALLER,
        host,
        env=force_env(force) | {"PLAN_ONLY": "true" if dry_run else "false"},
        require_root=True,
        interpreter="python3",
        remote_subdirs=("build", "src"),
    )
