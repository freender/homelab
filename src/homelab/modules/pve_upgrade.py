from __future__ import annotations

import os
from pathlib import Path

from ..deploy import DeploySession
from ..hosts import default_registry
from ..module_support import feature_paused, simple_root_installer_deploy

FEATURE = "pve-upgrade"
REMOTE_ROOT = "/tmp/homelab-pve-upgrade"

# Set by `deploy --confirm-upgrade`. Passed out-of-band rather than as a deploy()
# parameter so the gate does not change the signature every other module shares.
CONFIRM_ENV = "HOMELAB_CONFIRM_UPGRADE"


def confirmed() -> bool:
    return os.environ.get(CONFIRM_ENV, "").strip().lower() in {"1", "true", "yes"}


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    # This module dist-upgrades the host as the deploy action itself, so a bare
    # `deploy pve-upgrade <host>` is a live package change, not a config
    # convergence. Require the operator to say so. Dry-run stays ungated: it
    # changes nothing and is what validate's per-module smoke test exercises.
    if not dry_run and not confirmed():
        raise ValueError(
            "refusing to dist-upgrade without --confirm-upgrade. "
            "This module upgrades packages on the target during the deploy; run "
            "it per the rolling runbook in pve-upgrade/README.md "
            "(osiris, bray, ace, clovis, one node at a time)."
        )

    def env_for_host(host: str) -> dict[str, str]:
        return {"PAUSED": "true" if paused(root, host) else "false"}

    def dry_run_details(host: str) -> list[str]:
        if paused(root, host):
            return ["[DRY-RUN] Paused; would skip apt dist-upgrade"]
        return [
            "[DRY-RUN] Would run apt-get update && dist-upgrade "
            f"on {host} (warns only on reboot-required, never reboots)"
        ]

    return simple_root_installer_deploy(
        root,
        requested_host,
        dry_run,
        force,
        session,
        feature=FEATURE,
        remote_root=REMOTE_ROOT,
        env_for_host=env_for_host,
        dry_run_details=dry_run_details,
    )


def paused(root: Path, host: str) -> bool:
    registry = default_registry(root)
    return feature_paused(registry, host, FEATURE)
