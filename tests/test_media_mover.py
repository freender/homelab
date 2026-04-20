from __future__ import annotations

import importlib.util
import sys
from pathlib import Path, PurePath

import pytest

from homelab.hosts import HostRegistry
from homelab.modules.media_mover import DEFAULT_MEDIA_MOVER_SCHEDULE, normalize_config


def load_media_mover_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "media-mover"
        / "templates"
        / "homelab-media-mover.py"
    )
    spec = importlib.util.spec_from_file_location("test_media_mover_runtime", module_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"failed to load media mover module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_config(module, tmp_path: Path):
    source_root = tmp_path / "cache"
    target_root = tmp_path / "archive"
    merged_root = tmp_path / "merged"
    source_root.mkdir()
    target_root.mkdir()
    merged_root.mkdir()
    return module.Config(
        source_root=source_root,
        target_root=target_root,
        merged_root=merged_root,
        plex_mount_root=PurePath("/data"),
        plex_url="http://example.invalid:32400",
        tautulli_config_path=tmp_path / "tautulli.ini",
        ignore_paths=(),
        managed_roots=("movies",),
        tautulli_url="http://example.invalid",
        tautulli_api_key="test-key",
        tautulli_lookback_days=1,
        frequent_budget_bytes=1,
        ondeck_enabled=False,
        ondeck_budget_bytes=1,
        ondeck_tv_prefetch_episodes=0,
        ondeck_include_movies=False,
        watchlist_enabled=False,
        watchlist_budget_bytes=1,
        cache_min_free_space_bytes=1,
        cache_target_free_space_bytes=2,
        min_file_age_seconds=0,
        loop_interval_seconds=1,
        state_file=tmp_path / "state.json",
        dry_run=False,
    )


def write_movie(config, folder_name: str = "Movie (2026) {tmdb-1}") -> Path:
    movie_dir = config.source_root / "movies" / folder_name
    movie_dir.mkdir(parents=True)
    movie_file = movie_dir / f"{folder_name}.mkv"
    movie_file.write_bytes(b"movie-data")
    return movie_file


def test_run_once_keeps_synced_media_on_cache_for_normal_mover(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = load_media_mover_module()
    config = make_config(module, tmp_path)
    source_file = write_movie(config)

    monkeypatch.setattr(module, "try_build_hot_scores", lambda _config: ({}, True))
    monkeypatch.setattr(module, "file_is_open", lambda _path: False)

    exit_code = module.run_once(config, module.parse_args([]))

    assert exit_code == 0
    assert source_file.exists()
    target_file = config.target_root / source_file.relative_to(config.source_root)
    assert target_file.exists()
    output = capsys.readouterr().out
    assert "copied:" in output
    assert "evicted cache file:" not in output


def test_run_once_immediately_evicts_non_frequent_media_for_mover_now(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = load_media_mover_module()
    config = make_config(module, tmp_path)
    source_file = write_movie(config)

    monkeypatch.setattr(module, "try_build_hot_scores", lambda _config: ({}, True))
    monkeypatch.setattr(module, "file_is_open", lambda _path: False)

    exit_code = module.run_once(config, module.parse_args(["--sync-only", "--demote-non-frequent"]))

    assert exit_code == 0
    assert not source_file.exists()
    target_file = config.target_root / source_file.relative_to(config.source_root)
    assert target_file.exists()
    output = capsys.readouterr().out
    assert "copied:" in output
    assert "evicted cache file:" in output


def write_hosts_conf(tmp_path: Path, body: str) -> HostRegistry:
    path = tmp_path / "hosts.conf"
    path.write_text(body.lstrip(), encoding="utf-8")
    return HostRegistry(path)


def test_normalize_config_allows_missing_schedule_when_timer_disabled(tmp_path: Path) -> None:
    registry = write_hosts_conf(
        tmp_path,
        """
        ace:
          config:
            type: pve
            hostname: ace.internal
            user: root
            sshkey: infra
          features:
            media-mover:
              manage_timer: false
              source_dir: /cache/media
              target_dir: /user0/media
              merged_root: /user/media
        """,
    )

    config = normalize_config(registry, "ace")

    assert config.manage_timer is False
    assert config.schedule == DEFAULT_MEDIA_MOVER_SCHEDULE


def test_normalize_config_requires_schedule_when_timer_enabled(tmp_path: Path) -> None:
    registry = write_hosts_conf(
        tmp_path,
        """
        ace:
          config:
            type: pve
            hostname: ace.internal
            user: root
            sshkey: infra
          features:
            media-mover:
              manage_timer: true
              source_dir: /cache/media
              target_dir: /user0/media
              merged_root: /user/media
        """,
    )

    with pytest.raises(ValueError, match="media-mover.schedule is required for ace"):
        normalize_config(registry, "ace")


def test_normalize_config_converts_daily_schedule_to_full_calendar(tmp_path: Path) -> None:
    registry = write_hosts_conf(
        tmp_path,
        """
        ace:
          config:
            type: pve
            hostname: ace.internal
            user: root
            sshkey: infra
          features:
            media-mover:
              manage_timer: true
              schedule: daily
              source_dir: /cache/media
              target_dir: /user0/media
              merged_root: /user/media
        """,
    )

    config = normalize_config(registry, "ace")

    assert config.schedule == DEFAULT_MEDIA_MOVER_SCHEDULE
