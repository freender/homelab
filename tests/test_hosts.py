from __future__ import annotations

from pathlib import Path

import pytest

from homelab.hosts import HostLookupError, HostRegistry, validate_hosts_data


@pytest.fixture
def hosts_file(tmp_path: Path) -> Path:
    path = tmp_path / "hosts.conf"
    path.write_text(
        """
ace:
  config:
    type: pve
    hostname: ace.internal
    user: root
    sshkey: infra
  features:
    docker:
    ssh-config: {}
bray:
  config:
    type: ubuntu
    hostname: bray.internal
    user: root
    sshkey: homelab
  features:
    docker:
    apt-upgrade:
      autoupgrade: true
nullbox:
  config:
    type: ubuntu
    hostname: nullbox.internal
    user: root
    sshkey: homelab
  features:
    docker:
      backup: true
disabled:
  config:
    type: pve
    hostname: disabled.internal
    user: root
    sshkey: infra
  features:
    pve-postinstall:
      enabled: false
      timezone: UTC
    docker: false
    keepalived:
      deploy: false
      priority: 100
""".lstrip(),
        encoding="utf-8",
    )
    return path


def test_list_hosts_and_feature_filtering(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)

    assert registry.list_hosts() == ["ace", "bray", "nullbox", "disabled"]
    assert registry.list_hosts(feature="docker") == ["ace", "bray", "nullbox"]
    assert registry.list_hosts(feature="ssh-config") == ["ace"]
    assert registry.list_hosts(feature="pve-postinstall") == []
    assert registry.list_hosts(feature="keepalived") == []


def test_has_and_get_support_top_level_and_feature_keys(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)

    assert registry.has("ace", "ssh-config") is True
    assert registry.get("ace", "config.sshkey") == "infra"
    assert registry.has("bray", "docker") is True
    assert registry.get("ace", "config.type") == "pve"
    assert registry.get("bray", "apt-upgrade.autoupgrade") is True
    assert registry.has("disabled", "pve-postinstall") is False
    assert registry.get("disabled", "pve-postinstall.timezone") == "UTC"
    assert registry.has("disabled", "keepalived") is False
    assert registry.get("disabled", "keepalived.priority") == 100


def test_deploy_false_gate_does_not_warn(hosts_file: Path) -> None:
    import warnings

    registry = HostRegistry(hosts_file)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # keepalived uses `deploy: false`; must not emit the legacy warning.
        assert registry.has("disabled", "keepalived") is False


def test_legacy_enabled_false_warns(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)
    with pytest.warns(DeprecationWarning, match="deploy: false"):
        assert registry.has("disabled", "pve-postinstall") is False


def test_deploy_true_overrides_and_keeps_host(tmp_path: Path) -> None:
    path = tmp_path / "hosts.conf"
    path.write_text(
        """
ace:
  config:
    type: pve
    hostname: ace.internal
    user: root
    sshkey: infra
  features:
    docker:
      deploy: true
      enabled: false
""".lstrip(),
        encoding="utf-8",
    )
    registry = HostRegistry(path)
    # `deploy` wins over legacy `enabled`; host stays a target.
    assert registry.list_hosts(feature="docker") == ["ace"]


def test_feature_paused_reads_flag_and_default(hosts_file: Path) -> None:
    from homelab.module_support import feature_paused

    registry = HostRegistry(hosts_file)
    # No paused key -> default False.
    assert feature_paused(registry, "ace", "docker") is False


def test_feature_paused_true(tmp_path: Path) -> None:
    from homelab.module_support import feature_paused

    path = tmp_path / "hosts.conf"
    path.write_text(
        """
ace:
  config:
    type: pve
    hostname: ace.internal
    user: root
    sshkey: infra
  features:
    disk-spindown:
      paused: true
""".lstrip(),
        encoding="utf-8",
    )
    registry = HostRegistry(path)
    assert feature_paused(registry, "ace", "disk-spindown") is True
    # Paused host is still a deploy target (distinct from deploy: false).
    assert registry.list_hosts(feature="disk-spindown") == ["ace"]


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

def test_filter_hosts_returns_requested_subset(hosts_file: Path) -> None:
    registry = HostRegistry(hosts_file)
    supported = ["ace", "bray"]

    assert registry.filter_hosts("all", supported) == supported
    assert registry.filter_hosts("ace", supported) == ["ace"]
    assert registry.filter_hosts("nullbox", supported) == []
    with pytest.raises(HostLookupError, match="unknown host 'orbit'"):
        registry.filter_hosts("orbit", supported)


def test_registry_rejects_non_mapping_hosts_file(tmp_path: Path) -> None:
    path = tmp_path / "hosts.conf"
    path.write_text("- ace\n- bray\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hosts file must contain a mapping"):
        HostRegistry(path).load()


def test_registry_rejects_non_mapping_host_entry(tmp_path: Path) -> None:
    path = tmp_path / "hosts.conf"
    path.write_text("ace: nope\n", encoding="utf-8")

    with pytest.raises(ValueError, match="host entry must be a mapping: ace"):
        HostRegistry(path).load()


def test_validate_hosts_data_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    path = tmp_path / "hosts.conf"

    with pytest.raises(ValueError, match=r"unknown top-level key\(s\) for ace: typo"):
        validate_hosts_data(
            {
                "ace": {
                    "config": {
                        "type": "pve",
                        "hostname": "ace.internal",
                        "user": "root",
                        "sshkey": "infra",
                    },
                    "typo": True,
                }
            },
            path,
        )


def test_validate_hosts_data_rejects_missing_required_config_key(tmp_path: Path) -> None:
    path = tmp_path / "hosts.conf"

    with pytest.raises(ValueError, match=r"missing required config key\(s\) for ace: sshkey"):
        validate_hosts_data(
            {
                "ace": {
                    "config": {
                        "type": "pve",
                        "hostname": "ace.internal",
                        "user": "root",
                    }
                }
            },
            path,
        )


def test_validate_hosts_data_accepts_null_features(tmp_path: Path) -> None:
    path = tmp_path / "hosts.conf"

    validate_hosts_data(
        {
            "xur": {
                "config": {
                    "type": "pbs",
                    "hostname": "xur.internal",
                    "user": "root",
                    "sshkey": "infra",
                },
                "features": None,
            }
        },
        path,
    )
