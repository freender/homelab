from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..deploy import DeploySession
from .apcupsd import deploy as deploy_apcupsd
from .apt_upgrade import deploy as deploy_apt_upgrade
from .disk_spindown import deploy as deploy_disk_spindown
from .docker import deploy as deploy_docker
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
from .pve_realtek_r8152_dkms import deploy as deploy_pve_realtek_r8152_dkms
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


MODULES: dict[str, ModuleDefinition] = {
    "apcupsd": ModuleDefinition(
        name="apcupsd",
        deploy=deploy_apcupsd,
    ),
    "apt-upgrade": ModuleDefinition(
        name="APT Dist-Upgrade",
        deploy=deploy_apt_upgrade,
    ),
    "docker": ModuleDefinition(
        name="Docker Management Scripts",
        deploy=deploy_docker,
    ),
    "disk-spindown": ModuleDefinition(
        name="Disk Spindown",
        deploy=deploy_disk_spindown,
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
    "pve-realtek-r8152-dkms": ModuleDefinition(
        name="PVE Realtek r8152 DKMS Driver",
        deploy=deploy_pve_realtek_r8152_dkms,
    ),
    "pve-upgrade": ModuleDefinition(
        name="PVE/PBS/PDM Upgrade",
        deploy=deploy_pve_upgrade,
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
    "pve-interface-pinning",
    "pve-postinstall",
    "pve-notifications",
    "pve-postinstall-webhook",
    "pve-autoinstall",
    "pve-realtek-r8152-dkms",
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
    "disk-spindown",
    "pve-gpu-passthrough",
    "keepalived",
    "ssh-config",
    "wsl-conf",
    "ubuntu-setup",
    "zfs-automation",
    "docker",
    "apt-upgrade",
    "pve-upgrade",
]


def ordered_modules() -> list[str]:
    ordered = [name for name in MODULE_ORDER if name in MODULES]
    extras = sorted(name for name in MODULES if name not in MODULE_ORDER)
    return [*ordered, *extras]
