from __future__ import annotations

import importlib.util
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
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
        ondeck_enabled=False,
        ondeck_budget_bytes=1,
        ondeck_tv_prefetch_episodes=0,
        ondeck_include_movies=False,
        ondeck_movie_max_age_days=30,
        ondeck_series_max_age_days=60,
        recent_movie_retention_days=14,
        recent_tv_retention_days=14,
        cache_min_free_space_bytes=1,
        cache_target_free_space_bytes=2,
        min_file_age_seconds=0,
        loop_interval_seconds=1,
        state_file=tmp_path / "state.json",
        dry_run=False,
    )


def write_movie(
    config,
    folder_name: str = "Movie (2026) {tmdb-1}",
    content: bytes = b"movie-data",
) -> Path:
    movie_dir = config.source_root / "movies" / folder_name
    movie_dir.mkdir(parents=True)
    movie_file = movie_dir / f"{folder_name}.mkv"
    movie_file.write_bytes(content)
    return movie_file


def write_archive_movie(config, folder_name: str, content: bytes = b"movie-data") -> Path:
    movie_dir = config.target_root / "movies" / folder_name
    movie_dir.mkdir(parents=True)
    movie_file = movie_dir / f"{folder_name}.mkv"
    movie_file.write_bytes(content)
    return movie_file


def write_text_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_episode(
    root: Path,
    show_name: str,
    season_name: str,
    episode_name: str,
    content: bytes = b"episode-data",
) -> Path:
    episode_dir = root / "tv" / show_name / season_name
    episode_dir.mkdir(parents=True, exist_ok=True)
    episode_file = episode_dir / episode_name
    episode_file.write_bytes(content)
    return episode_file


def test_run_once_keeps_synced_media_on_cache_for_normal_mover(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = load_media_mover_module()
    config = make_config(module, tmp_path)
    source_file = write_movie(config)

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
    config = replace(
        make_config(module, tmp_path),
        recent_movie_retention_days=0,
        recent_tv_retention_days=0,
    )
    source_file = write_movie(config)

    monkeypatch.setattr(module, "file_is_open", lambda _path: False)

    exit_code = module.run_once(config, module.parse_args(["--sync-only", "--demote-non-frequent"]))

    assert exit_code == 0
    assert not source_file.exists()
    target_file = config.target_root / source_file.relative_to(config.source_root)
    assert target_file.exists()
    output = capsys.readouterr().out
    assert "copied:" in output
    assert "evicted cache file:" in output


def test_manual_drain_keeps_recent_cached_media(tmp_path: Path, monkeypatch, capsys) -> None:
    module = load_media_mover_module()
    config = replace(make_config(module, tmp_path), recent_movie_retention_days=7)
    recent_source = write_movie(config, "Recent Movie (2026) {tmdb-1}")
    stale_source = write_movie(config, "Stale Movie (2026) {tmdb-2}")
    stale_time = time.time() - 10 * 86400
    os.utime(stale_source, (stale_time, stale_time))

    monkeypatch.setattr(module, "file_is_open", lambda _path: False)

    exit_code = module.run_once(config, module.parse_args(["--sync-only", "--demote-non-frequent"]))

    assert exit_code == 0
    assert recent_source.exists()
    assert not stale_source.exists()
    assert (config.target_root / recent_source.relative_to(config.source_root)).exists()
    assert (config.target_root / stale_source.relative_to(config.source_root)).exists()
    output = capsys.readouterr().out
    assert "manual mode: demoting cached media outside recent and On Deck protection" in output
    assert "evicted cache file:" in output


def test_cleanup_respects_ignored_paths(tmp_path: Path, monkeypatch) -> None:
    module = load_media_mover_module()
    config = replace(
        make_config(module, tmp_path),
        ignore_paths=(tmp_path / "cache" / "downloads",),
    )
    ignored_dir = config.source_root / "downloads" / "usenet" / "temp"
    ignored_dir.mkdir(parents=True)
    stale_temp = ignored_dir / f"keep{module.TEMP_SUFFIX}"
    stale_temp.write_text("temp", encoding="utf-8")
    stale_time = time.time() - 2 * 86400
    os.utime(stale_temp, (stale_time, stale_time))

    monkeypatch.setattr(module, "file_is_open", lambda _path: False)

    module.cleanup_stale_temp_files(config.source_root, config.ignore_paths)
    module.prune_empty_dirs(config.source_root, config.ignore_paths)

    assert stale_temp.exists()
    assert ignored_dir.exists()


def test_run_once_does_not_reclaim_between_min_and_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = load_media_mover_module()
    config = replace(
        make_config(module, tmp_path),
        recent_movie_retention_days=0,
        recent_tv_retention_days=0,
        cache_min_free_space_bytes=5,
        cache_target_free_space_bytes=6,
    )
    source_file = write_movie(config, "Buffered Movie (2026) {tmdb-9}")

    monkeypatch.setattr(module, "file_is_open", lambda _path: False)
    monkeypatch.setattr(module, "filesystem_usage", lambda _path: (100, 95, 5))

    exit_code = module.run_once(config, module.parse_args([]))

    assert exit_code == 0
    assert source_file.exists()
    assert (config.target_root / source_file.relative_to(config.source_root)).exists()
    output = capsys.readouterr().out
    assert "evicted cache file:" not in output


def test_sync_directory_keeps_archive_video_when_cache_only_has_subtitle(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_media_mover_module()
    config = make_config(module, tmp_path)
    folder_name = "Subtitle Only Movie (2026) {tmdb-3}"
    archive_video = write_archive_movie(config, folder_name)
    cache_subtitle = write_text_file(
        config.source_root / "movies" / folder_name / f"{folder_name}.en.srt",
        "subtitle",
    )

    monkeypatch.setattr(module, "file_is_open", lambda _path: False)

    result = module.sync_directory(Path("movies") / folder_name, config)

    assert result.conflicts == 0
    assert archive_video.exists()
    assert (config.target_root / cache_subtitle.relative_to(config.source_root)).exists()


def test_sync_tv_unit_keeps_archive_video_when_cache_only_has_subtitle(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_media_mover_module()
    config = replace(make_config(module, tmp_path), managed_roots=("movies", "tv"))
    show_name = "Example Show (2026) {tvdb-1}"
    archive_video = write_episode(
        config.target_root,
        show_name,
        "Season 1",
        f"{show_name} - S01E10.mkv",
        b"video-data",
    )
    cache_subtitle = write_text_file(
        config.source_root / "tv" / show_name / "Season 1" / f"{show_name} - S01E10.en.srt",
        "subtitle",
    )
    unit = Path(f"tv/{show_name}/Season 1/__episodes__/{show_name}|S01|10")

    monkeypatch.setattr(module, "file_is_open", lambda _path: False)

    result = module.sync_tv_unit(unit, config)

    assert result.conflicts == 0
    assert archive_video.exists()
    assert (config.target_root / cache_subtitle.relative_to(config.source_root)).exists()


def test_copy_file_replaces_same_name_when_size_differs(tmp_path: Path) -> None:
    module = load_media_mover_module()
    config = make_config(module, tmp_path)
    folder_name = "Replace Movie (2026) {tmdb-4}"
    source_file = write_text_file(
        config.source_root / "movies" / folder_name / f"{folder_name}.nfo",
        "new metadata with different size",
    )
    target_file = write_text_file(
        config.target_root / "movies" / folder_name / f"{folder_name}.nfo",
        "old",
    )

    copied_bytes = module.copy_file(source_file, config.source_root, config.target_root)

    assert copied_bytes == source_file.stat().st_size
    assert target_file.read_text(encoding="utf-8") == source_file.read_text(encoding="utf-8")


def test_report_cache_effectiveness_shows_watched_cache_hit_rates(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = load_media_mover_module()
    config = replace(
        make_config(module, tmp_path),
        ondeck_enabled=True,
        ondeck_budget_bytes=1024,
    )
    cached_file = write_movie(config, "Cached Movie (2026) {tmdb-1}")
    archive_file = write_archive_movie(config, "Archive Movie (2026) {tmdb-2}")
    cached_unit = cached_file.relative_to(config.source_root).parent
    archive_unit = archive_file.relative_to(config.target_root).parent

    monkeypatch.setattr(module, "file_is_open", lambda _path: False)
    monkeypatch.setattr(
        module,
        "try_collect_ondeck_entries",
        lambda _config: [
            module.OnDeckEntry(
                relative_dir=cached_unit,
                score=100,
                item_type="movie",
                age_days=12.0,
                progress_percent=50.0,
                current=True,
            ),
            module.OnDeckEntry(
                relative_dir=archive_unit,
                score=10,
                item_type="movie",
                age_days=12.0,
                progress_percent=50.0,
                current=False,
            )
        ],
    )
    monkeypatch.setattr(module, "filesystem_usage", lambda _path: (1000, 400, 600))

    exit_code = module.report_cache_effectiveness(
        config,
        module.parse_args(["--report-cache-effectiveness", "--report-limit", "2"]),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "report: desired_ondeck_units=2" in output
    assert "desired_ondeck_cached_units=1" in output
    assert "desired_ondeck_hit_rate=50.0%" in output
    assert "desired_ondeck_byte_hit_rate=50.0%" in output
    assert "recent_cached_units=1" in output
    assert (
        "ondeck_age: movie_age_limit_days=30 series_age_limit_days=60 "
        "current_movies=1 current_episodes=0" in output
    )
    assert "movie_median_age_days=12.0" in output
    assert f"top_ondeck: rank=1 location=cache score=100 size=10B unit={cached_unit}" in output
    assert f"top_ondeck: rank=2 location=archive score=10 size=10B unit={archive_unit}" in output


def test_run_once_reclaims_headroom_before_ondeck_promotion(tmp_path: Path, monkeypatch) -> None:
    module = load_media_mover_module()
    config = replace(
        make_config(module, tmp_path),
        ondeck_enabled=True,
        ondeck_budget_bytes=32,
        recent_movie_retention_days=0,
        recent_tv_retention_days=0,
        cache_min_free_space_bytes=5,
        cache_target_free_space_bytes=6,
    )
    stale_cached = write_movie(config, "Stale Cached (2026) {tmdb-1}", b"123")
    archive_ondeck = write_archive_movie(config, "On Deck Archive (2026) {tmdb-2}", b"45")
    stale_unit = stale_cached.relative_to(config.source_root).parent
    archive_unit = archive_ondeck.relative_to(config.target_root).parent
    stale_time = time.time() - 10 * 86400
    os.utime(stale_cached, (stale_time, stale_time))

    monkeypatch.setattr(module, "file_is_open", lambda _path: False)
    monkeypatch.setattr(
        module,
        "try_collect_ondeck_entries",
        lambda _config: [
            module.OnDeckEntry(
                relative_dir=archive_unit,
                score=100,
                item_type="movie",
                age_days=1.0,
                progress_percent=50.0,
                current=True,
            )
        ],
    )
    monkeypatch.setattr(module, "filesystem_usage", lambda _path: (100, 96, 4))

    exit_code = module.run_once(config, module.parse_args([]))

    assert exit_code == 0
    assert not stale_cached.exists()
    assert (config.source_root / archive_ondeck.relative_to(config.target_root)).exists()
    assert (config.target_root / stale_cached.relative_to(config.source_root)).exists()
    cached_units = {
        unit
        for unit, stats in module.collect_all_unit_stats(config).items()
        if stats.size_on_cache > 0
    }
    assert stale_unit not in cached_units


def test_collect_all_unit_stats_uses_episode_units_for_tv(tmp_path: Path) -> None:
    module = load_media_mover_module()
    config = replace(make_config(module, tmp_path), managed_roots=("movies", "tv"))
    episode_one = write_episode(
        config.source_root,
        "Example Show (2026) {tvdb-1}",
        "Season 1",
        "Example Show (2026) {tvdb-1} - S01E01.mkv",
        b"one",
    )
    episode_two = write_episode(
        config.source_root,
        "Example Show (2026) {tvdb-1}",
        "Season 1",
        "Example Show (2026) {tvdb-1} - S01E02.mkv",
        b"two-two",
    )

    stats = module.collect_all_unit_stats(config)

    unit_one = Path(
        "tv/Example Show (2026) {tvdb-1}/Season 1/__episodes__/"
        "Example Show (2026) {tvdb-1}|S01|01"
    )
    unit_two = Path(
        "tv/Example Show (2026) {tvdb-1}/Season 1/__episodes__/"
        "Example Show (2026) {tvdb-1}|S01|02"
    )
    assert stats[unit_one].size_on_cache == episode_one.stat().st_size
    assert stats[unit_two].size_on_cache == episode_two.stat().st_size


def test_build_ondeck_scores_prefetches_into_next_season(tmp_path: Path, monkeypatch) -> None:
    module = load_media_mover_module()
    config = replace(
        make_config(module, tmp_path),
        managed_roots=("movies", "tv"),
        ondeck_enabled=True,
        ondeck_tv_prefetch_episodes=3,
    )
    show_name = "Example Show (2026) {tvdb-1}"
    season_one_ep_three = write_episode(
        config.target_root,
        show_name,
        "Season 1",
        f"{show_name} - S01E03.mkv",
    )
    season_one_ep_four = write_episode(
        config.target_root,
        show_name,
        "Season 1",
        f"{show_name} - S01E04.mkv",
    )
    season_two_ep_one = write_episode(
        config.target_root,
        show_name,
        "Season 2",
        f"{show_name} - S02E01.mkv",
    )
    season_two_ep_two = write_episode(
        config.target_root,
        show_name,
        "Season 2",
        f"{show_name} - S02E02.mkv",
    )

    current_xml = ET.fromstring(
        f"""
        <MediaContainer>
          <Video type=\"episode\" grandparentRatingKey=\"show-1\" parentIndex=\"1\" index=\"3\">
            <Media>
              <Part file=\"/data/tv/{show_name}/Season 1/{season_one_ep_three.name}\" />
            </Media>
          </Video>
        </MediaContainer>
        """
    )
    leaves_xml = ET.fromstring(
        f"""
        <MediaContainer>
          <Video parentIndex=\"1\" index=\"3\">
            <Media>
              <Part file=\"/data/tv/{show_name}/Season 1/{season_one_ep_three.name}\" />
            </Media>
          </Video>
          <Video parentIndex=\"1\" index=\"4\">
            <Media><Part file=\"/data/tv/{show_name}/Season 1/{season_one_ep_four.name}\" /></Media>
          </Video>
          <Video parentIndex=\"2\" index=\"1\">
            <Media><Part file=\"/data/tv/{show_name}/Season 2/{season_two_ep_one.name}\" /></Media>
          </Video>
          <Video parentIndex=\"2\" index=\"2\">
            <Media><Part file=\"/data/tv/{show_name}/Season 2/{season_two_ep_two.name}\" /></Media>
          </Video>
        </MediaContainer>
        """
    )

    monkeypatch.setattr(
        module,
        "load_plex_context",
        lambda _config: module.PlexContext(
            server_url="http://example.invalid:32400",
            admin_token="token",
            user_tokens={"user": "token"},
        ),
    )

    def fake_plex_get_xml(url: str, _token: str):
        if url.endswith("/library/onDeck"):
            return current_xml
        if url.endswith("/library/metadata/show-1/allLeaves"):
            return leaves_xml
        raise AssertionError(url)

    monkeypatch.setattr(module, "plex_get_xml", fake_plex_get_xml)

    scores = module.build_ondeck_scores(config)

    current_unit = Path(f"tv/{show_name}/Season 1/__episodes__/{show_name}|S01|03")
    next_same_season = Path(f"tv/{show_name}/Season 1/__episodes__/{show_name}|S01|04")
    next_season_one = Path(f"tv/{show_name}/Season 2/__episodes__/{show_name}|S02|01")
    next_season_two = Path(f"tv/{show_name}/Season 2/__episodes__/{show_name}|S02|02")
    assert scores[current_unit] == 100
    assert scores[next_same_season] == 10
    assert scores[next_season_one] == 10
    assert scores[next_season_two] == 10


def test_build_ondeck_scores_skips_stale_movies(tmp_path: Path, monkeypatch) -> None:
    module = load_media_mover_module()
    config = replace(
        make_config(module, tmp_path),
        managed_roots=("movies", "tv"),
        ondeck_enabled=True,
        ondeck_include_movies=True,
        ondeck_movie_max_age_days=30,
    )
    recent_movie = write_archive_movie(config, "Recent Movie (2026) {tmdb-1}")
    stale_movie = write_archive_movie(config, "Stale Movie (2026) {tmdb-2}")
    now = 1_700_000_000
    recent_last_viewed = now - 5 * 86400
    stale_last_viewed = now - 90 * 86400

    monkeypatch.setattr(module.time, "time", lambda: float(now))
    monkeypatch.setattr(
        module,
        "load_plex_context",
        lambda _config: module.PlexContext(
            server_url="http://example.invalid:32400",
            admin_token="token",
            user_tokens={"user": "token"},
        ),
    )
    monkeypatch.setattr(
        module,
        "plex_get_xml",
        lambda url, _token: ET.fromstring(
            f"""
            <MediaContainer>
              <Video
                type=\"movie\"
                title=\"Recent Movie\"
                lastViewedAt=\"{recent_last_viewed}\"
                duration=\"1000\"
                viewOffset=\"500\"
              >
                <Media>
                  <Part file=\"/data/movies/Recent Movie (2026) {{tmdb-1}}/{recent_movie.name}\" />
                </Media>
              </Video>
              <Video
                type=\"movie\"
                title=\"Stale Movie\"
                lastViewedAt=\"{stale_last_viewed}\"
                duration=\"1000\"
                viewOffset=\"500\"
              >
                <Media>
                  <Part file=\"/data/movies/Stale Movie (2026) {{tmdb-2}}/{stale_movie.name}\" />
                </Media>
              </Video>
            </MediaContainer>
            """
        ),
    )

    scores = module.build_ondeck_scores(config)

    recent_unit = Path("movies/Recent Movie (2026) {tmdb-1}")
    stale_unit = Path("movies/Stale Movie (2026) {tmdb-2}")
    assert scores[recent_unit] == 100
    assert stale_unit not in scores


def test_build_ondeck_scores_skips_stale_series_and_prefetch(tmp_path: Path, monkeypatch) -> None:
    module = load_media_mover_module()
    config = replace(
        make_config(module, tmp_path),
        managed_roots=("movies", "tv"),
        ondeck_enabled=True,
        ondeck_tv_prefetch_episodes=2,
        ondeck_series_max_age_days=60,
    )
    show_name = "Example Show (2026) {tvdb-1}"
    current_episode = write_episode(
        config.target_root,
        show_name,
        "Season 1",
        f"{show_name} - S01E03.mkv",
    )
    next_episode = write_episode(
        config.target_root,
        show_name,
        "Season 1",
        f"{show_name} - S01E04.mkv",
    )
    now = 1_700_000_000
    stale_last_viewed = now - 90 * 86400
    monkeypatch.setattr(module.time, "time", lambda: float(now))
    monkeypatch.setattr(
        module,
        "load_plex_context",
        lambda _config: module.PlexContext(
            server_url="http://example.invalid:32400",
            admin_token="token",
            user_tokens={"user": "token"},
        ),
    )
    current_xml = ET.fromstring(
        f"""
        <MediaContainer>
          <Video
            type=\"episode\"
            grandparentRatingKey=\"show-1\"
            parentIndex=\"1\"
            index=\"3\"
            lastViewedAt=\"{stale_last_viewed}\"
            duration=\"1000\"
            viewOffset=\"500\"
          >
            <Media><Part file=\"/data/tv/{show_name}/Season 1/{current_episode.name}\" /></Media>
          </Video>
        </MediaContainer>
        """
    )
    leaves_xml = ET.fromstring(
        f"""
        <MediaContainer>
          <Video parentIndex=\"1\" index=\"3\">
            <Media><Part file=\"/data/tv/{show_name}/Season 1/{current_episode.name}\" /></Media>
          </Video>
          <Video parentIndex=\"1\" index=\"4\">
            <Media><Part file=\"/data/tv/{show_name}/Season 1/{next_episode.name}\" /></Media>
          </Video>
        </MediaContainer>
        """
    )

    def fake_plex_get_xml(url: str, _token: str):
        if url.endswith("/library/onDeck"):
            return current_xml
        if url.endswith("/library/metadata/show-1/allLeaves"):
            return leaves_xml
        raise AssertionError(url)

    monkeypatch.setattr(module, "plex_get_xml", fake_plex_get_xml)

    scores = module.build_ondeck_scores(config)

    current_unit = Path(f"tv/{show_name}/Season 1/__episodes__/{show_name}|S01|03")
    next_unit = Path(f"tv/{show_name}/Season 1/__episodes__/{show_name}|S01|04")
    assert current_unit not in scores
    assert next_unit not in scores


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
