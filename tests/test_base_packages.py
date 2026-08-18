"""Guards for base-packages.

Two things are worth pinning. First, the baseline actually reaches every
apt-managed host: this module exists because `mbuffer`/`vim`/`mc` were installed
by `pve-postinstall` and therefore guaranteed on four hosts out of fourteen, and
a host silently dropping out of the feature set would recreate that gap without
any visible failure. Second, `mbuffer` must survive in the baseline, because
`zfs-automation`'s replication jobs pipe through it -- removing it would break
replication on the next rebuild, not at deploy time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab.hosts import default_registry
from homelab.modules import MODULE_ORDER, base_packages

ROOT = Path(__file__).resolve().parents[1]

HOSTS_HEADER = """\
{host}:
  config:
    type: ubuntu
    hostname: {host}.internal
    user: root
    sshkey: infra
  features:
"""


def write_hosts(tmp_path: Path, body: str) -> Path:
    (tmp_path / "hosts.conf").write_text(body, encoding="utf-8")
    return tmp_path


def test_baseline_includes_mbuffer_for_zfs_replication() -> None:
    # zfs-automation pipes zfs send/recv through mbuffer. It used to come from
    # pve-postinstall; base-packages owns it now and must keep it.
    assert "mbuffer" in base_packages.BASE_PACKAGES
    assert "ripgrep" in base_packages.BASE_PACKAGES


def test_runs_before_zfs_automation() -> None:
    # Ordering is the reason mbuffer is safe to move here: the package has to be
    # installed before the replication units that use it are written.
    assert MODULE_ORDER.index("base-packages") < MODULE_ORDER.index("zfs-automation")
    assert MODULE_ORDER[0] == "base-packages"


def test_enabled_on_every_apt_managed_host() -> None:
    """Every host that apt can reach must carry the feature.

    macOS (exo) and the Arch-based Pi-KVM appliance are the only legitimate
    exclusions; anything else missing is the drift this module was written to
    stop.
    """
    registry = default_registry(ROOT)
    non_apt = {"exo", "pi-kvm"}
    expected = {host for host in registry.list_hosts() if host not in non_apt}
    actual = set(registry.list_hosts(feature="base-packages"))
    assert actual == expected, f"missing base-packages: {sorted(expected - actual)}"


def test_non_apt_hosts_are_excluded() -> None:
    registry = default_registry(ROOT)
    enabled = set(registry.list_hosts(feature="base-packages"))
    assert "exo" not in enabled
    assert "pi-kvm" not in enabled


def test_extra_packages_appended_after_baseline(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="tower")
        + "    base-packages:\n      extra:\n        - smartmontools\n",
    )
    packages = base_packages.packages_for_host(root, "tower")
    assert packages[: len(base_packages.BASE_PACKAGES)] == list(base_packages.BASE_PACKAGES)
    assert packages[-1] == "smartmontools"


def test_extra_does_not_duplicate_baseline(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="tower")
        + "    base-packages:\n      extra:\n        - vim\n",
    )
    packages = base_packages.packages_for_host(root, "tower")
    assert packages.count("vim") == 1


def test_bare_feature_gets_the_baseline(tmp_path: Path) -> None:
    root = write_hosts(tmp_path, HOSTS_HEADER.format(host="tower") + "    base-packages:\n")
    assert base_packages.packages_for_host(root, "tower") == list(base_packages.BASE_PACKAGES)


def test_bad_extra_type_is_rejected(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="tower") + "    base-packages:\n      extra: 5\n",
    )
    with pytest.raises(ValueError):
        base_packages.packages_for_host(root, "tower")
