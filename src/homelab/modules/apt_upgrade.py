from __future__ import annotations

from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import HostLookupError, default_registry
from ..module_support import feature_paused, normalize_bool, run_module_deploy
from ..output import print_sub
from ..ssh import HostConnection

REMOTE_ROOT = "/tmp/homelab-apt-upgrade"
SERVICE_NAME = "homelab-apt-dist-upgrade.service"
TIMER_NAME = "homelab-apt-dist-upgrade.timer"
AUTO_REBOOT_PATH = "/etc/apt/apt.conf.d/53homelab-auto-reboot"
DEFAULT_AUTO_REBOOT_TIME = "now"


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
        "apt-upgrade",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
    )


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    registry = default_registry(root)
    try:
        host_type = registry.get(host, "config.type")
    except HostLookupError as exc:
        raise ValueError(str(exc)) from exc

    if host_type != "ubuntu":
        print_sub(f"Skipping {host}: apt-upgrade supports type ubuntu only")
        return

    autoupgrade_enabled = normalize_autoupgrade(registry, host)
    autoupgrade = "true" if autoupgrade_enabled else "false"
    schedule = str(registry.get(host, "apt-upgrade.schedule", "*-*-* 09:00:00"))
    paused = feature_paused(registry, host, "apt-upgrade")
    auto_reboot = normalize_auto_reboot(registry, host)
    auto_reboot_time = str(
        registry.get(host, "apt-upgrade.auto_reboot_time", DEFAULT_AUTO_REBOOT_TIME)
    )

    build_dir = root / "apt-upgrade" / "build" / host
    prepare_build_dir(build_dir)
    write_service(build_dir, cleanup=False)
    if autoupgrade == "true":
        write_timer(build_dir, schedule)
    if auto_reboot:
        write_auto_reboot_conf(build_dir, auto_reboot_time)
    write_env(
        build_dir,
        autoupgrade=autoupgrade,
        schedule=schedule,
        paused=paused,
        auto_reboot=auto_reboot,
    )

    ssh_hostname = str(registry.get(host, "config.hostname", host))
    ssh_user = str(registry.get(host, "config.user"))
    connection = HostConnection(host, user=ssh_user, hostname=ssh_hostname)
    print_sub("Comparing with remote configs...")
    _, message = connection.remote_diff(
        build_dir / "service",
        f"/etc/systemd/system/{SERVICE_NAME}",
    )
    print_sub(message)
    if autoupgrade == "true":
        _, message = connection.remote_diff(
            build_dir / "timer",
            f"/etc/systemd/system/{TIMER_NAME}",
        )
        print_sub(message)
    if auto_reboot:
        _, message = connection.remote_diff(build_dir / "auto-reboot.conf", AUTO_REBOOT_PATH)
        print_sub(message)

    if dry_run:
        if paused:
            print_sub(
                f"[DRY-RUN] Would pause apt-upgrade on {host} "
                "(stop and disable the timer, skip on-demand run)"
            )
        elif autoupgrade == "true":
            print_sub(
                f"[DRY-RUN] Would install apt dist-upgrade timer on {host} at {schedule}"
            )
        else:
            print_sub(f"[DRY-RUN] Would run apt dist-upgrade on {host} (on-demand only)")
        if auto_reboot:
            print_sub(
                f"[DRY-RUN] Would let unattended-upgrades reboot {host} "
                f"when a run leaves /var/run/reboot-required (at {auto_reboot_time})"
            )
        else:
            print_sub(f"[DRY-RUN] Would ensure {host} never reboots itself")
        return

    stage_and_install(root, build_dir, connection, force=force)


def normalize_autoupgrade(registry, host: str) -> bool:
    return normalize_bool(
        registry.get(host, "apt-upgrade.autoupgrade", None),
        False,
        f"apt-upgrade.autoupgrade must be true or false for {host}",
    )


def normalize_auto_reboot(registry, host: str) -> bool:
    """Opt-in unattended reboot, default false.

    Only ever true for hosts where an unattended reboot is acceptable. The PVE
    nodes are not candidates -- they are not on this module at all, and their
    reboots belong to the pve-upgrade runbook. tower/helm/neo/riven are on this
    module but must stay false: they carry HA Traefik/keepalived, the monitoring
    stack, and the OpenCode server respectively.
    """
    return normalize_bool(
        registry.get(host, "apt-upgrade.auto_reboot", None),
        False,
        f"apt-upgrade.auto_reboot must be true or false for {host}",
    )


def write_auto_reboot_conf(build_dir: Path, reboot_time: str) -> None:
    """Hand the reboot decision to unattended-upgrades rather than reimplementing it.

    These hosts already run stock unattended-upgrades (apt-daily-upgrade.timer
    plus APT::Periodic::Unattended-Upgrade "1"), so the reboot mechanism is
    already present and only its keys are unset. Setting them here is the
    supported way to do this: u-u checks /var/run/reboot-required at the end of
    its run and reboots if the flag is present, regardless of which tool created
    it -- so a kernel installed by this module's own dist-upgrade timer is
    picked up by the next u-u run.

    "now" means "as soon as that run finishes", not "immediately on boot". It is
    used in preference to a fixed clock time because u-u schedules a clock time
    with `shutdown -r <time>`, which silently rolls to the next day if the run
    finishes after that time -- a real race against apt-daily-upgrade.timer's
    randomised 06:00-07:00 window.
    """
    content = "\n".join(
        [
            "// Managed by homelab (apt-upgrade) -- do not edit on the host.",
            "//",
            "// Unattended reboot is opt-in per host via apt-upgrade.auto_reboot in",
            "// hosts.conf. It is set only for hosts that carry no HA role and no",
            "// singleton service the rest of the homelab depends on.",
            'Unattended-Upgrade::Automatic-Reboot "true";',
            "",
            "// A logged-in admin must not silently veto the reboot; these are",
            "// unattended offsite hosts and an interactive session is incidental.",
            'Unattended-Upgrade::Automatic-Reboot-WithUsers "true";',
            "",
            f'Unattended-Upgrade::Automatic-Reboot-Time "{reboot_time}";',
            "",
        ]
    )
    (build_dir / "auto-reboot.conf").write_text(content, encoding="utf-8")


def write_service(build_dir: Path, cleanup: bool) -> None:
    lines = [
        "[Unit]",
        "Description=Homelab daily apt update and dist-upgrade",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=/usr/bin/apt-get update",
        "ExecStart=/usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y dist-upgrade",
    ]
    if cleanup:
        lines.extend(
            [
                (
                    "ExecStart=/usr/bin/env DEBIAN_FRONTEND=noninteractive "
                    "/usr/bin/apt-get -y autoremove"
                ),
                "ExecStart=/usr/bin/apt-get -y autoclean",
            ]
        )
    (build_dir / "service").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_timer(build_dir: Path, schedule: str) -> None:
    content = "\n".join(
        [
            "[Unit]",
            "Description=Run homelab daily apt update and dist-upgrade",
            "",
            "[Timer]",
            f"OnCalendar={schedule}",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    (build_dir / "timer").write_text(content, encoding="utf-8")


def write_env(
    build_dir: Path,
    autoupgrade: str,
    schedule: str,
    paused: bool,
    auto_reboot: bool = False,
) -> None:
    write_env_file(
        build_dir / "env",
        {
            "CLEANUP": "false",
            "AUTOUPGRADE": autoupgrade,
            "SCHEDULE": schedule,
            "PAUSED": "true" if paused else "false",
            "AUTO_REBOOT": "true" if auto_reboot else "false",
        },
    )


def stage_and_install(root: Path, build_dir: Path, connection: HostConnection, force: bool) -> None:
    upload_paths: list[tuple[Path, str]] = [
        (root / "apt-upgrade" / "scripts", f"{REMOTE_ROOT}/scripts")
    ]
    for file_name in ["service", "env", "timer", "auto-reboot.conf"]:
        file_path = build_dir / file_name
        if file_path.is_file():
            upload_paths.append((file_path, f"{REMOTE_ROOT}/build/{file_name}"))

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        upload_paths,
        "scripts/install.sh",
        env=force_env(force),
        require_root=True,
        remote_subdirs=("build", "lib", "scripts"),
    )
