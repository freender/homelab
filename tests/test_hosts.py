from __future__ import annotations

from pathlib import Path

import pytest

from homelab.hosts import HostLookupError, HostRegistry


@pytest.fixture
def hosts_file(tmp_path: Path) -> Path:
    path = tmp_path / "hosts.conf"
    path.write_text(
        """
ace:
  config:
    type: pve
    sshkey: infra
  features:
    docker:
    ssh-config: {}
bray:
  config:
    type: ubuntu
  features:
    docker:
    apt-upgrade:
      autoupgrade: true
nullbox:
  config:
    type: ubuntu
  features:
    docker:
      backup: true
badhost: nope
""".lstrip(),
        encoding="utf-8",
    )
    return path


def test_list_hosts_and_feature_filtering(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)

    assert registry.list_hosts() == ["ace", "bray", "nullbox", "badhost"]
    assert registry.list_hosts(feature="docker") == ["ace", "bray", "nullbox"]
    assert registry.list_hosts(feature="ssh-config") == ["ace"]


def test_has_and_get_support_top_level_and_feature_keys(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)

    assert registry.has("ace", "ssh-config") is True
    assert registry.get("ace", "config.sshkey") == "infra"
    assert registry.has("bray", "docker") is True
    assert registry.get("ace", "config.type") == "pve"
    assert registry.get("bray", "apt-upgrade.autoupgrade") is True


def test_get_returns_default_for_missing_or_none_values(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)

    assert registry.get("nullbox", "docker.owner", "1000") == "1000"
    assert registry.get("ace", "config.group", "staff") == "staff"


def test_get_and_host_lookup_raise_clear_errors(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)

    with pytest.raises(HostLookupError, match="unknown host 'orbit'"):
        registry.get("orbit", "config.type")

    with pytest.raises(HostLookupError, match="missing key 'config.group' for host 'ace'"):
        registry.get("ace", "config.group")

    with pytest.raises(ValueError, match="host entry must be a mapping: badhost"):
        registry.has("badhost", "docker")


def test_filter_hosts_returns_requested_subset(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)
    supported = ["ace", "bray"]

    assert registry.filter_hosts("all", supported) == supported
    assert registry.filter_hosts("ace", supported) == ["ace"]
    assert registry.filter_hosts("orbit", supported) == []


def test_registry_rejects_non_mapping_hosts_file(tmp_path: Path) -> None:
    path = tmp_path / "hosts.conf"
    path.write_text("- ace\n- bray\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hosts file must contain a mapping"):
        HostRegistry(path).load()
