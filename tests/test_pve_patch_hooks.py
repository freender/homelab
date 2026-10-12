"""Regression guards for deferred PVE patch hooks."""

import re
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

    assert "TARGET_CHECKSUM_DIR=/var/lib/homelab/pve-patches/target-checksums/" in text
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


def _checksum_dir(installer: str) -> str:
    text = (ROOT / installer).read_text(encoding="utf-8")
    match = re.search(r"^TARGET_CHECKSUM_DIR=(\S+)$", text, re.MULTILINE)
    assert match, installer
    return match.group(1)


def test_each_patch_owns_its_target_checksum_record() -> None:
    # Two patches of ZFSPoolPlugin.pm once shared one checksum file keyed by
    # basename; after an upgrade the first reapply recorded the new checksum and
    # the second skipped as "unchanged", leaving its patch off (ace/clovis, Oct 2026).
    dirs = [_checksum_dir(installer) for installer in PATCH_INSTALLERS]

    assert len(set(dirs)) == len(dirs)
    for installer, checksum_dir in zip(PATCH_INSTALLERS, dirs, strict=True):
        state_id = re.search(
            r"^STATE_DIR=/var/lib/homelab/(\S+)$",
            (ROOT / installer).read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert state_id, installer
        assert checksum_dir == f"/var/lib/homelab/pve-patches/target-checksums/{state_id.group(1)}"


@pytest.mark.parametrize("installer", PATCH_INSTALLERS)
def test_installer_removes_the_shared_legacy_checksum_records(installer: str) -> None:
    text = (ROOT / installer).read_text(encoding="utf-8")
    installer_body = text.split("<<'SCRIPT'", 1)[0]
    targets = re.findall(r"^\w*TARGET=(/usr/share/perl5/\S+)$", text, re.MULTILINE)
    assert targets, installer

    assert "LEGACY_CHECKSUM_DIR=/var/lib/homelab/pve-patches/target-checksums\n" in installer_body
    for target in targets:
        legacy = f'"${{LEGACY_CHECKSUM_DIR}}/{Path(target).name}.checksum"'
        assert legacy in installer_body, (installer, target)
