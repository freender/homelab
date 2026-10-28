"""Unit tests for `zfs_automation.pools`.

`resolve_pools` picks which pools the module scrubs. Get it wrong and a pool
silently stops being scrubbed -- there is no error, just no scrub.
"""

from __future__ import annotations

from pathlib import Path

from homelab.hosts import HostRegistry
from homelab.modules.zfs_automation import pools as p


def registry_from(body: str, tmp_path: Path) -> HostRegistry:
    path = tmp_path / "hosts.conf"
    path.write_text(body.lstrip(), encoding="utf-8")
    return HostRegistry(path)


def pve_host(name: str, feature_block: str = "") -> str:
    body = (
        f"{name}:\n"
        "  config:\n"
        "    type: pve\n"
        f"    hostname: {name}.internal\n"
        "    user: root\n"
        "    sshkey: infra\n"
        "  features:\n"
    )
    return body + (feature_block or "    {}\n")


def test_unique_pools_dedupes_and_keeps_first_seen_order() -> None:
    datasets = ["tank/a", "cache/b", "tank/c", "rpool/d", "cache/e"]

    assert p.unique_pools(datasets) == ["tank", "cache", "rpool"]


def test_unique_pools_of_nothing_is_empty() -> None:
    assert p.unique_pools([]) == []


def test_resolve_pools_prefers_an_explicit_list(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            "    zfs-automation:\n"
            "      pools:\n"
            "        - tank\n"
            "        - rpool\n"
            "      snapshot_plans:\n"
            "        - dataset: cache/appdata\n",
        ),
        tmp_path,
    )

    # cache/appdata would otherwise contribute `cache`; the explicit list wins.
    assert p.resolve_pools(registry, "ace") == ["tank", "rpool"]


def test_resolve_pools_derives_from_snapshot_datasets(tmp_path: Path) -> None:
    registry = registry_from(
        pve_host(
            "ace",
            "    zfs-automation:\n"
            "      snapshot_plans:\n"
            "        - dataset: cache/appdata\n"
            "        - dataset: tank/media\n"
            "        - dataset: cache/other\n",
        ),
        tmp_path,
    )

    assert p.resolve_pools(registry, "ace") == ["cache", "tank"]


def test_resolve_pools_falls_back_to_cache_when_nothing_is_configured(tmp_path: Path) -> None:
    registry = registry_from(pve_host("ace", "    zfs-automation:\n      scrub: true\n"), tmp_path)

    assert p.resolve_pools(registry, "ace") == ["cache"]
