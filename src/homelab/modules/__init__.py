from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..deploy import DeploySession
from .apcupsd import deploy as deploy_apcupsd
from .apt_upgrade import deploy as deploy_apt_upgrade
from .base_packages import deploy as deploy_base_packages
from .docker import deploy as deploy_docker
from .docker_stacks import deploy as deploy_docker_stacks
from .keepalived import deploy as deploy_keepalived
from .metrics_exporters import deploy as deploy_metrics_exporters
from .monitoring_config import deploy as deploy_monitoring_config
from .pbs_client_backup import deploy as deploy_pbs_client_backup
from .pve_autoinstall import deploy as deploy_pve_autoinstall
from .pve_backup import deploy as deploy_pve_backup
from .pve_gpu_passthrough import deploy as deploy_pve_gpu_passthrough
from .pve_http_boot import deploy as deploy_pve_http_boot
from .pve_interface_pinning import deploy as deploy_pve_interface_pinning
from .pve_lxc_pre_replication_patch import deploy as deploy_pve_lxc_pre_replication_patch
from .pve_notifications import deploy as deploy_pve_notifications
from .pve_postinstall import deploy as deploy_pve_postinstall
from .pve_postinstall_webhook import deploy as deploy_pve_postinstall_webhook
from .pve_upgrade import deploy as deploy_pve_upgrade
from .pve_zfs_large_block_patch import deploy as deploy_pve_zfs_large_block_patch
from .pve_zfs_migration_sync_patch import deploy as deploy_pve_zfs_migration_sync_patch
from .ssh_config import deploy as deploy_ssh_config
from .ubuntu_setup import deploy as deploy_ubuntu_setup
from .vmalert_rules import deploy as deploy_vmalert_rules
from .wsl_conf import deploy as deploy_wsl_conf
from .zfs_automation import deploy as deploy_zfs_automation


@dataclass(frozen=True)
class ModuleDefinition:
    name: str
    deploy: Callable[[Path, str, bool, bool, DeploySession], int]
    # False for modules that mutate host state at deploy time rather than
    # converging config, so sweeping them up in `deploy all` would perform an
    # unrequested live change. Excluding by omission from MODULE_ORDER is not
    # enough: ordered_modules() appends unlisted modules as extras precisely so
    # a newly added one is never silently skipped.
    include_in_all: bool = True


MODULES: dict[str, ModuleDefinition] = {
    "apcupsd": ModuleDefinition(
        name="apcupsd",
        deploy=deploy_apcupsd,
    ),
    "apt-upgrade": ModuleDefinition(
        name="APT Dist-Upgrade",
        deploy=deploy_apt_upgrade,
    ),
    "base-packages": ModuleDefinition(
        name="Base Packages",
        deploy=deploy_base_packages,
    ),
    "docker": ModuleDefinition(
        name="Docker Management Scripts",
        deploy=deploy_docker,
    ),
    "docker-stacks": ModuleDefinition(
        name="Docker Compose Stacks",
        deploy=deploy_docker_stacks,
    ),
    "keepalived": ModuleDefinition(
        name="Keepalived",
        deploy=deploy_keepalived,
    ),
    "pbs-client-backup": ModuleDefinition(
        name="PBS Client Backup",
        deploy=deploy_pbs_client_backup,
    ),
    "pve-backup": ModuleDefinition(
        name="PVE Backup",
        deploy=deploy_pve_backup,
    ),
    "metrics-exporters": ModuleDefinition(
        name="Prometheus Metrics Exporters",
        deploy=deploy_metrics_exporters,
    ),
    "monitoring-config": ModuleDefinition(
        name="VictoriaMetrics and Alertmanager Config",
        deploy=deploy_monitoring_config,
    ),
    "pve-autoinstall": ModuleDefinition(
        name="PVE Automated Install (PDM Answers)",
        deploy=deploy_pve_autoinstall,
    ),
    "pve-postinstall": ModuleDefinition(
        name="PVE Post-Install Configs",
        deploy=deploy_pve_postinstall,
    ),
    "pve-notifications": ModuleDefinition(
        name="PVE Notifications",
        deploy=deploy_pve_notifications,
    ),
    "pve-postinstall-webhook": ModuleDefinition(
        name="PVE Post-Install Webhook",
        deploy=deploy_pve_postinstall_webhook,
    ),
    "pve-http-boot": ModuleDefinition(
        name="PVE HTTP Boot",
        deploy=deploy_pve_http_boot,
    ),
    "pve-upgrade": ModuleDefinition(
        name="PVE/PBS/PDM Upgrade",
        deploy=deploy_pve_upgrade,
        # Runs apt-get dist-upgrade on the host during the deploy itself, unlike
        # apt-upgrade which only installs a timer. Deploying "everything" must
        # not dist-upgrade the cluster, and doing so ignores the ordering and
        # preflight the runbook requires. Explicit target + --confirm-upgrade.
        include_in_all=False,
    ),
    "pve-interface-pinning": ModuleDefinition(
        name="PVE Interface Pinning",
        deploy=deploy_pve_interface_pinning,
    ),
    "pve-lxc-pre-replication-patch": ModuleDefinition(
        name="PVE LXC Pre-Replication Patch",
        deploy=deploy_pve_lxc_pre_replication_patch,
    ),
    "pve-gpu-passthrough": ModuleDefinition(
        name="GPU Passthrough Configs",
        deploy=deploy_pve_gpu_passthrough,
    ),
    "pve-zfs-large-block-patch": ModuleDefinition(
        name="PVE ZFS Large-Block Patch",
        deploy=deploy_pve_zfs_large_block_patch,
    ),
    "pve-zfs-migration-sync-patch": ModuleDefinition(
        name="PVE ZFS Migration Sync Patch",
        deploy=deploy_pve_zfs_migration_sync_patch,
    ),
    "ssh-config": ModuleDefinition(name="SSH Config", deploy=deploy_ssh_config),
    "ubuntu-setup": ModuleDefinition(name="Ubuntu OS Setup", deploy=deploy_ubuntu_setup),
    "vmalert-rules": ModuleDefinition(name="vmalert Rules", deploy=deploy_vmalert_rules),
    "wsl-conf": ModuleDefinition(name="WSL Conf", deploy=deploy_wsl_conf),
    "zfs-automation": ModuleDefinition(name="ZFS Automation", deploy=deploy_zfs_automation),
}

MODULE_ORDER = [
    # First: later modules assume the baseline tools exist. zfs-automation's
    # replication pipes through mbuffer, which used to be installed by
    # pve-postinstall and so was only ever guaranteed on the four PVE nodes.
    "base-packages",
    "pve-interface-pinning",
    "pve-postinstall",
    "pve-notifications",
    "pve-postinstall-webhook",
    "pve-autoinstall",
    "pve-http-boot",
    # Install the host archive/key before its restore consumer. pve-backup also
    # persists its own staged key so a direct module deploy remains rebuild-safe.
    "pbs-client-backup",
    "pve-backup",
    "apcupsd",
    "metrics-exporters",
    "monitoring-config",
    "vmalert-rules",
    "pve-zfs-large-block-patch",
    "pve-zfs-migration-sync-patch",
    "pve-lxc-pre-replication-patch",
    "pve-gpu-passthrough",
    "keepalived",
    "ssh-config",
    "wsl-conf",
    "ubuntu-setup",
    "zfs-automation",
    "docker",
    # Engine, swarm membership and the overlay must exist before any stack that
    # attaches to net_overlay is reconciled.
    "docker-stacks",
    # apt-upgrade last, after everything that could install a package it would
    # then upgrade. It was preceded by apt-security-updates until that module
    # was archived; apt-upgrade is now the single apt mechanism for the fleet.
    "apt-upgrade",
    # pve-upgrade is deliberately absent: it is include_in_all=False and is
    # driven by the rolling runbook, not by deploy order.
]


def all_registered_modules() -> list[str]:
    """Every registered module in deploy order, including `deploy all` exclusions.

    Use for exhaustive checks (dry-run smoke, registry cross-checks) that must
    still cover modules `deploy all` refuses to run.
    """
    ordered = [name for name in MODULE_ORDER if name in MODULES]
    extras = sorted(name for name in MODULES if name not in MODULE_ORDER)
    return [*ordered, *extras]


def ordered_modules() -> list[str]:
    """Modules `deploy all` runs, in order."""
    return [name for name in all_registered_modules() if MODULES[name].include_in_all]
