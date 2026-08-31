"""Regression guards for deferred PVE patch hooks."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCH_INSTALLERS = (
    "pve-zfs-large-block-patch/scripts/install.sh",
    "pve-zfs-migration-sync-patch/scripts/install.sh",
    "pve-lxc-pre-replication-patch/scripts/install.sh",
)


@pytest.mark.parametrize("installer", PATCH_INSTALLERS)
def test_deferred_patch_hook_only_runs_when_its_target_changed(installer: str) -> None:
    text = (ROOT / installer).read_text(encoding="utf-8")

    assert "TARGET_CHECKSUM_DIR=/var/lib/homelab/pve-patches/target-checksums" in text
    assert "cksum < \"${target}\"" in text
    assert "[[ -f ${target} && -f ${checksum_file} ]] || return 1" in text
    assert "--if-target-changed" in text
    assert "unchanged; skipping" in text


@pytest.mark.parametrize("installer", PATCH_INSTALLERS)
def test_direct_patch_deploy_remains_unconditional(installer: str) -> None:
    text = (ROOT / installer).read_text(encoding="utf-8")

    # The hook is gated, but the installer itself must converge a missing patch.
    direct_invocation = '"${PATCH_SCRIPT}"'
    if "lxc-" in installer:
        direct_invocation += " --restart-services"
    assert text.rstrip().endswith(direct_invocation)
