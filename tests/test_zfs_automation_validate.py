"""Unit tests for `zfs_automation.validate()`.

These build a throwaway repo root around the real module files, so each branch
is reached without depending on the real hosts.conf.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab.modules import zfs_automation

REPO = Path(__file__).resolve().parents[1]

HOSTS = """
ace:
  config:
    type: pve
    hostname: ace.internal
    user: root
    sshkey: infra
  features:
    zfs-automation:
      pools: {pools}
"""


def make_root(tmp_path: Path, pools: str = "[vm-flash]", *, module: bool = True) -> Path:
    if module:
        (tmp_path / "zfs-automation").symlink_to(REPO / "zfs-automation")
    (tmp_path / "hosts.conf").write_text(HOSTS.format(pools=pools).lstrip(), encoding="utf-8")
    return tmp_path


def test_validate_accepts_a_host_with_a_managed_pool(tmp_path: Path) -> None:
    assert zfs_automation.validate(make_root(tmp_path), ["ace"]) is None


def test_validate_requires_at_least_one_managed_pool(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one managed pool for ace"):
        zfs_automation.validate(make_root(tmp_path, "[]"), ["ace"])


def test_validate_requires_the_static_config_files(tmp_path: Path) -> None:
    root = make_root(tmp_path, module=False)

    with pytest.raises(ValueError, match="missing required config"):
        zfs_automation.validate(root, ["ace"])


def test_validate_requires_the_template_files(tmp_path: Path) -> None:
    root = make_root(tmp_path, module=False)
    (root / "zfs-automation").mkdir()
    (root / "zfs-automation" / "configs").symlink_to(REPO / "zfs-automation" / "configs")

    with pytest.raises(ValueError, match="missing required template"):
        zfs_automation.validate(root, ["ace"])
