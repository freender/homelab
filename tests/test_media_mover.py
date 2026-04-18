from __future__ import annotations

import importlib.util
import sys
from pathlib import Path, PurePath


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
        ignore_paths=(),
        managed_roots=("movies",),
        tautulli_url="http://example.invalid",
        tautulli_api_key="test-key",
        tautulli_lookback_days=1,
        frequent_budget_bytes=1,
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
