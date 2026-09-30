from __future__ import annotations

import subprocess

import pytest

from homelab.hosts import HostLookupError
from homelab.modules import pve_postinstall as pp

_MISSING = object()


class FakeRegistry:
    """Dict-backed registry stub that mirrors HostRegistry.get's sentinel-default
    and raise-on-missing-without-default behavior (src/homelab/hosts.py:76)."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, host: str, key: str, default: object = _MISSING) -> object:
        if key in self._values:
            return self._values[key]
        if default is not _MISSING:
            return default
        raise HostLookupError(f"missing key '{key}' for host '{host}'")


def test_host_settings_clustered_host_uses_mgmt_ip_for_link0() -> None:
    registry = FakeRegistry(
        {
            "config.type": "pve",
            "pve-postinstall.interfaces.mgmt_ip": "10.0.10.5/24",
        }
    )

    settings = pp._host_settings(registry, "ace")

    assert settings.host_type == "pve"
    assert settings.timezone == "UTC"
    assert settings.import_pools == ""
    assert settings.mounts == ""
    assert settings.expected_clustered == "true"
    assert settings.cluster_link0 == "10.0.10.5"


def test_host_settings_clustered_host_without_mgmt_ip_falls_back_to_hostname() -> None:
    registry = FakeRegistry(
        {
            "config.type": "pve",
            "config.hostname": "ace.freender.internal",
        }
    )

    settings = pp._host_settings(registry, "ace")

    assert settings.expected_clustered == "true"
    assert settings.cluster_link0 == "ace.freender.internal"


def test_host_settings_standalone_host_has_no_cluster_link0() -> None:
    registry = FakeRegistry(
        {
            "config.type": "pve",
            "config.standalone": True,
            "pve-postinstall.interfaces.mgmt_ip": "10.0.10.5/24",
        }
    )

    settings = pp._host_settings(registry, "osiris")

    assert settings.expected_clustered == "false"
    assert settings.cluster_link0 == ""


def test_host_settings_reads_timezone_import_pools_and_mounts() -> None:
    registry = FakeRegistry(
        {
            "config.type": "pve",
            "config.standalone": True,
            "pve-postinstall.timezone": "America/New_York",
            "pve-postinstall.import_pools": ["tank", "cache"],
            "pve-postinstall.mounts": [
                {"label": "media", "path": "/mnt/media"},
                {"label": "backup", "path": "/mnt/backup"},
            ],
        }
    )

    settings = pp._host_settings(registry, "ace")

    assert settings.timezone == "America/New_York"
    assert settings.import_pools == "tank cache"
    assert settings.mounts == "media:/mnt/media backup:/mnt/backup"


def test_host_settings_missing_config_type_raises_value_error() -> None:
    registry = FakeRegistry({})

    with pytest.raises(ValueError, match="missing key 'config.type'"):
        pp._host_settings(registry, "ace")


def test_host_settings_rejects_non_pve_host_type_before_reading_anything_else() -> None:
    # Only config.type is answerable; every other lookup would raise
    # HostLookupError if _host_settings reached it. The rejection at the top
    # must fire first (see AGENTS.md: cheap checks before expensive work).
    registry = FakeRegistry({"config.type": "ubuntu"})

    with pytest.raises(ValueError, match="Unsupported host type for ace: ubuntu"):
        pp._host_settings(registry, "ace")


def test_host_settings_rejects_non_list_import_pools() -> None:
    registry = FakeRegistry({"config.type": "pve", "pve-postinstall.import_pools": "tank"})

    with pytest.raises(ValueError, match="import_pools must be a list"):
        pp._host_settings(registry, "ace")


def test_host_settings_rejects_non_list_mounts() -> None:
    registry = FakeRegistry({"config.type": "pve", "pve-postinstall.mounts": "media"})

    with pytest.raises(ValueError, match="mounts must be a list"):
        pp._host_settings(registry, "ace")


def test_host_settings_rejects_mount_entry_missing_label_or_path() -> None:
    registry = FakeRegistry(
        {"config.type": "pve", "pve-postinstall.mounts": [{"label": "media"}]}
    )

    with pytest.raises(ValueError, match="must have label and path"):
        pp._host_settings(registry, "ace")


def test_host_settings_rejects_invalid_standalone_value() -> None:
    registry = FakeRegistry({"config.type": "pve", "config.standalone": "sideways"})

    with pytest.raises(ValueError, match="config.standalone must be true or false"):
        pp._host_settings(registry, "ace")


# --- cluster rejoin helper: the delnode guard --------------------------------------


def _rejoin_guard(tmp_path, members: str) -> subprocess.CompletedProcess:
    """Run only the helper's membership guard, against a fake /etc/pve/.members."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pp.build_cluster_rejoin_helper(root, tmp_path)
    script = (tmp_path / "homelab-pve-cluster-rejoin-helper").read_text(encoding="utf-8")
    start = script.index("# Never delnode a live member")
    end = script.index('echo "==> Cleaning stale cluster state')
    members_file = tmp_path / "members.json"
    members_file.write_text(members, encoding="utf-8")
    guard = script[start:end].replace("/etc/pve/.members", str(members_file))
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nnode=ace\n{guard}\necho PROCEED"],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "members",
    ['{"nodelist": {"ace": {"online": 1}}}', "not json", ""],
    ids=["online", "unreadable", "empty"],
)
def test_rejoin_helper_refuses_to_delnode_a_live_or_unknown_member(tmp_path, members) -> None:
    result = _rejoin_guard(tmp_path, members)

    assert result.returncode == 1
    assert "PROCEED" not in result.stdout
    assert "Refusing to clean up ace" in result.stderr


@pytest.mark.parametrize(
    "members",
    ['{"nodelist": {"ace": {"online": 0}}}', '{"nodelist": {"bray": {"online": 1}}}'],
    ids=["offline", "absent"],
)
def test_rejoin_helper_proceeds_for_an_offline_or_absent_node(tmp_path, members) -> None:
    result = _rejoin_guard(tmp_path, members)

    assert result.returncode == 0
    assert "PROCEED" in result.stdout
