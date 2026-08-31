"""Guards for apt-security-updates.

The module's whole value is that it cannot touch Proxmox packages. Two things
enforce that: the origin scope in the shipped apt config, and the refusal to
coexist with apt-upgrade (which dist-upgrades everything and would make the
narrow scope meaningless). Both are asserted here, because a regression in
either is silent on the host -- the config still looks right while the resolved
policy is wider than intended.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab.modules import apt_security_updates

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "apt-security-updates" / "configs"

HOSTS_HEADER = """\
{host}:
  config:
    type: pve
    hostname: {host}.internal
    user: root
    sshkey: infra
  features:
"""


def write_hosts(tmp_path: Path, blocks: dict[str, list[str]]) -> Path:
    text = ""
    for host, features in blocks.items():
        text += HOSTS_HEADER.format(host=host)
        for feature in features:
            text += f"    {feature}:\n"
    (tmp_path / "hosts.conf").write_text(text, encoding="utf-8")
    return tmp_path


def stage_module(tmp_path: Path) -> None:
    configs = tmp_path / "apt-security-updates" / "configs"
    configs.mkdir(parents=True)
    for spec in apt_security_updates.FILE_SPECS:
        (configs / spec.build_name).write_text("// test\n", encoding="utf-8")
    scripts = tmp_path / "apt-security-updates" / "scripts"
    scripts.mkdir()
    (scripts / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")


def test_validate_rejects_a_host_that_also_runs_apt_upgrade(tmp_path: Path) -> None:
    stage_module(tmp_path)
    write_hosts(tmp_path, {"ace": ["apt-security-updates", "apt-upgrade"]})

    with pytest.raises(ValueError, match="apt-security-updates and apt-upgrade"):
        apt_security_updates.validate(tmp_path)


def test_validate_accepts_the_features_on_separate_hosts(tmp_path: Path) -> None:
    stage_module(tmp_path)
    write_hosts(
        tmp_path,
        {"ace": ["apt-security-updates"], "tower": ["apt-upgrade"]},
    )

    apt_security_updates.validate(tmp_path)
    assert apt_security_updates.conflicting_hosts(tmp_path) == []


def test_real_inventory_keeps_the_two_features_disjoint() -> None:
    """The live hosts.conf must never enable both on one host."""
    assert apt_security_updates.conflicting_hosts(ROOT) == []


def test_origin_pattern_is_debian_security_only() -> None:
    text = (CONFIGS / "52homelab-security-updates").read_text(encoding="utf-8")

    # #clear is load-bearing: APT appends to lists, so without it the packaged
    # defaults stay active alongside ours and the scope widens instead.
    assert "#clear Unattended-Upgrade::Origins-Pattern;" in text
    assert "#clear Unattended-Upgrade::Package-Blacklist;" in text

    assert '"origin=Debian,codename=${distro_codename}-security,label=Debian-Security";' in text

    # Nothing may pull from the Proxmox repo, stable-updates, or the backports
    # suite that metrics-exporters enables on these same hosts.
    directives = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith('"') and line.strip().endswith('";')
    ]
    origins = [d for d in directives if "origin=" in d]
    assert len(origins) == 1, origins
    for forbidden in ("Proxmox", "stable-updates", "backports"):
        assert forbidden not in origins[0]


def test_reboot_is_never_automatic() -> None:
    """A hypervisor must never reboot itself; that is pve-upgrade's runbook."""
    text = (CONFIGS / "52homelab-security-updates").read_text(encoding="utf-8")
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in text
    assert 'Unattended-Upgrade::Automatic-Reboot-WithUsers "false";' in text
    assert 'Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";' in text


def test_installer_verifies_resolved_scope_not_just_the_file() -> None:
    """The #clear failure mode is silent, so the installer must re-check apt-config."""
    text = (ROOT / "apt-security-updates" / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "apt-config dump Unattended-Upgrade::Origins-Pattern" in text
    assert "verify_origins_scope || exit 1" in text
    assert "homelab-apt-dist-upgrade.timer" in text
