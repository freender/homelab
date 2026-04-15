from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..deploy import DeploySession
from .apcupsd import deploy as deploy_apcupsd
from .apt_upgrade import deploy as deploy_apt_upgrade
from .docker import deploy as deploy_docker
from .keepalived import deploy as deploy_keepalived
from .media_mover import deploy as deploy_media_mover
from .pve_backup import deploy as deploy_pve_backup
from .pve_exporters import deploy as deploy_pve_exporters
from .pve_gpu_passthrough import deploy as deploy_pve_gpu_passthrough
from .pve_lxc_mounts import deploy as deploy_pve_lxc_mounts
from .pve_postinstall import deploy as deploy_pve_postinstall
from .snapraid import deploy as deploy_snapraid
from .ssh_config import deploy as deploy_ssh_config
from .tiered_media import deploy as deploy_tiered_media
from .ubuntu_setup import deploy as deploy_ubuntu_setup
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
    "keepalived": ModuleDefinition(
        name="Keepalived",
        deploy=deploy_keepalived,
    ),
    "media-mover": ModuleDefinition(
        name="Media Mover",
        deploy=deploy_media_mover,
    ),
    "pve-backup": ModuleDefinition(
        name="PVE Backup",
        deploy=deploy_pve_backup,
    ),
    "pve-exporters": ModuleDefinition(
        name="PVE Prometheus Exporters",
        deploy=deploy_pve_exporters,
    ),
    "pve-postinstall": ModuleDefinition(
        name="PVE Post-Install Configs",
        deploy=deploy_pve_postinstall,
    ),
    "pve-gpu-passthrough": ModuleDefinition(
        name="GPU Passthrough Configs",
        deploy=deploy_pve_gpu_passthrough,
    ),
    "pve-lxc-mounts": ModuleDefinition(
        name="PVE LXC Mount Configs",
        deploy=deploy_pve_lxc_mounts,
    ),
    "ssh-config": ModuleDefinition(name="SSH Config", deploy=deploy_ssh_config),
    "snapraid": ModuleDefinition(name="SnapRAID", deploy=deploy_snapraid),
    "tiered-media": ModuleDefinition(name="Tiered Media", deploy=deploy_tiered_media),
    "ubuntu-setup": ModuleDefinition(name="Ubuntu OS Setup", deploy=deploy_ubuntu_setup),
    "zfs-automation": ModuleDefinition(name="ZFS Automation", deploy=deploy_zfs_automation),
}

MODULE_ORDER = [
    "pve-postinstall",
    "pve-backup",
    "apcupsd",
    "pve-exporters",
    "pve-gpu-passthrough",
    "keepalived",
    "media-mover",
    "snapraid",
    "tiered-media",
    "pve-lxc-mounts",
    "ssh-config",
    "ubuntu-setup",
    "zfs-automation",
    "docker",
    "apt-upgrade",
]


def ordered_modules() -> list[str]:
    ordered = [name for name in MODULE_ORDER if name in MODULES]
    extras = sorted(name for name in MODULES if name not in MODULE_ORDER)
    return [*ordered, *extras]
