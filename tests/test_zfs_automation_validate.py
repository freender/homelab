"""Unit tests for `zfs_automation.validate()`.

Its branches used to be reached only through the real hosts.conf. Once no host
carried `source_private_keys`, the secret-reference check went untested even
though it is what stops a deploy staging a key that doesn't exist in the
catalog. These tests build a throwaway repo root around the real module files.
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
      pools:
        - vm-flash
{extra}
"""

KEY_BLOCK = """      source_private_keys:
        - secret: {secret}
          path: /root/.ssh/push_ed25519
"""


def make_root(tmp_path: Path, extra: str = "", *, module: bool = True) -> Path:
    if module:
        (tmp_path / "zfs-automation").symlink_to(REPO / "zfs-automation")
    (tmp_path / "hosts.conf").write_text(HOSTS.format(extra=extra).lstrip(), encoding="utf-8")
    secrets = tmp_path / "secrets"
    (secrets / "templates").mkdir(parents=True)
    (secrets / "templates" / "known.env.tpl").write_text("KEY=op://v/i/f\n", encoding="utf-8")
    (secrets / "templates" / "known.env.tpl.example").write_text("KEY=x\n", encoding="utf-8")
    (secrets / "catalog.yml").write_text(
        "secrets:\n  known:\n    template: secrets/templates/known.env.tpl\n",
        encoding="utf-8",
    )
    return tmp_path


def test_validate_accepts_a_known_source_key_secret(tmp_path: Path) -> None:
    root = make_root(tmp_path, KEY_BLOCK.format(secret="known"))

    assert zfs_automation.validate(root, ["ace"]) is None


def test_validate_names_the_host_for_an_unknown_source_key_secret(tmp_path: Path) -> None:
    root = make_root(tmp_path, KEY_BLOCK.format(secret="retired-key"))

    with pytest.raises(ValueError, match="^ace: unknown secret 'retired-key'"):
        zfs_automation.validate(root, ["ace"])


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
