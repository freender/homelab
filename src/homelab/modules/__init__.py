from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..deploy import DeploySession
from .apcupsd import deploy as deploy_apcupsd
from .apt_upgrade import deploy as deploy_apt_upgrade
from .docker import deploy as deploy_docker
from .pve_backup import deploy as deploy_pve_backup
from .pve_exporters import deploy as deploy_pve_exporters
from .pve_gpu_passthrough import deploy as deploy_pve_gpu_passthrough
from .pve_postinstall import deploy as deploy_pve_postinstall
from .ssh_config import deploy as deploy_ssh_config


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
    "ssh-config": ModuleDefinition(name="SSH Config", deploy=deploy_ssh_config),
}

MODULE_ORDER = [
    "pve-postinstall",
    "pve-backup",
    "apcupsd",
    "pve-exporters",
    "pve-gpu-passthrough",
    "ssh-config",
    "docker",
    "apt-upgrade",
]


def ordered_modules() -> list[str]:
    ordered = [name for name in MODULE_ORDER if name in MODULES]
    extras = sorted(name for name in MODULES if name not in MODULE_ORDER)
    return [*ordered, *extras]
