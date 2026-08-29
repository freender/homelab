"""Guards for apt-upgrade's opt-in unattended reboot.

auto_reboot hands the reboot decision to unattended-upgrades rather than
reimplementing it, so the risk is not in the mechanism -- it is in the flag
reaching a host that must never reboot itself. These tests pin the default to
false, pin the live inventory to the two hosts that opted in, and pin the
generated apt config to the keys u-u actually reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab.hosts import default_registry
from homelab.modules import apt_upgrade

ROOT = Path(__file__).resolve().parents[1]

# Hosts that carry an HA role or a singleton the rest of the homelab depends on:
# tower is primary keepalived/Traefik and the media/storage host, helm is the
# whole monitoring stack, neo is tertiary keepalived, riven is the OpenCode
# server. A reboot flag landing on any of these is the regression to catch.
MUST_NEVER_AUTO_REBOOT = ("tower", "helm", "neo", "riven")

HOSTS_TEMPLATE = """\
{host}:
  config:
    type: ubuntu
    hostname: {host}.internal
    user: root
    sshkey: infra
  features:
    apt-upgrade:
      autoupgrade: true
{extra}"""


def registry_for(tmp_path: Path, host: str, extra: str = "") -> object:
    (tmp_path / "hosts.conf").write_text(
        HOSTS_TEMPLATE.format(host=host, extra=extra), encoding="utf-8"
    )
    return default_registry(tmp_path)


def test_auto_reboot_defaults_to_false(tmp_path: Path) -> None:
    registry = registry_for(tmp_path, "somehost")
    assert apt_upgrade.normalize_auto_reboot(registry, "somehost") is False


def test_auto_reboot_reads_the_flag(tmp_path: Path) -> None:
    registry = registry_for(tmp_path, "somehost", extra="      auto_reboot: true\n")
    assert apt_upgrade.normalize_auto_reboot(registry, "somehost") is True


def test_auto_reboot_rejects_a_non_boolean(tmp_path: Path) -> None:
    registry = registry_for(tmp_path, "somehost", extra="      auto_reboot: sometimes\n")
    with pytest.raises(ValueError, match="auto_reboot must be true or false"):
        apt_upgrade.normalize_auto_reboot(registry, "somehost")


def test_live_inventory_only_auto_reboots_the_offsite_hosts() -> None:
    """cottonwood and cinci opted in; nothing load-bearing may join them."""
    registry = default_registry(ROOT)
    enabled = [
        host
        for host in registry.list_hosts(feature="apt-upgrade")
        if apt_upgrade.normalize_auto_reboot(registry, host)
    ]
    assert sorted(enabled) == ["cinci", "cottonwood"]


def test_ha_and_singleton_hosts_never_auto_reboot() -> None:
    registry = default_registry(ROOT)
    for host in MUST_NEVER_AUTO_REBOOT:
        assert apt_upgrade.normalize_auto_reboot(registry, host) is False, host


def test_generated_conf_sets_the_keys_unattended_upgrades_reads(tmp_path: Path) -> None:
    apt_upgrade.write_auto_reboot_conf(tmp_path, apt_upgrade.DEFAULT_AUTO_REBOOT_TIME)
    text = (tmp_path / "auto-reboot.conf").read_text(encoding="utf-8")

    assert 'Unattended-Upgrade::Automatic-Reboot "true";' in text
    assert 'Unattended-Upgrade::Automatic-Reboot-WithUsers "true";' in text
    # "now" means "when the u-u run finishes". A clock time here would be
    # scheduled with `shutdown -r <time>` and roll to the next day whenever the
    # run overshoots it, racing apt-daily-upgrade.timer's randomised window.
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "now";' in text


def test_generated_conf_honours_a_custom_time(tmp_path: Path) -> None:
    apt_upgrade.write_auto_reboot_conf(tmp_path, "04:00")
    text = (tmp_path / "auto-reboot.conf").read_text(encoding="utf-8")
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "04:00";' in text


def test_env_carries_auto_reboot_to_the_installer(tmp_path: Path) -> None:
    apt_upgrade.write_env(
        tmp_path, autoupgrade="true", schedule="*-*-* 08:00:00", paused=False, auto_reboot=True
    )
    assert "AUTO_REBOOT=true" in (tmp_path / "env").read_text(encoding="utf-8")

    apt_upgrade.write_env(tmp_path, autoupgrade="true", schedule="*-*-* 08:00:00", paused=False)
    assert "AUTO_REBOOT=false" in (tmp_path / "env").read_text(encoding="utf-8")


def test_installer_removes_the_drop_in_when_disabled() -> None:
    """The flag must be reversible: no drop-in left behind when it is taken away."""
    text = (ROOT / "apt-upgrade" / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert 'if [[ "$AUTO_REBOOT" != "true" ]]; then' in text
    assert 'rm -f "$AUTO_REBOOT_PATH"' in text
    # Pause must also stop the host rebooting itself.
    assert 'AUTO_REBOOT="false"\n    apply_auto_reboot' in text
    # Verify the resolved policy, not just the written file.
    assert "apt-config dump Unattended-Upgrade::Automatic-Reboot" in text
    # The reboot only happens at the end of a u-u run, so its timer is required.
    assert "apt-daily-upgrade.timer" in text
