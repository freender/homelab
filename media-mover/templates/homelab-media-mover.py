#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePath

IGNORED_PREFIX = "."
IGNORED_SUFFIXES = (".part", ".tmp", ".partial", ".!qB")
TEMP_SUFFIX = ".homelab-media-mover.tmp"
MOVIE_ROOTS = {"movies", "movies4k"}
TV_ROOTS = {"tv", "tv4k"}
MOVIE_FOLDER_RE = re.compile(r"^.+ \((?P<year>\d{4})\) \{tmdb-(?P<tmdb>\d+)\}$")
EPISODE_RE = re.compile(r"^(?P<title>.+?) - S(?P<season>\d{2})E(?P<first>\d{2})(?:(?:E|-)(?P<second>\d{2}))?$")
OPERATIONS_LOCK = Path("/var/lib/homelab-media/operations.lock")


@dataclass(frozen=True)
class Config:
    source_root: Path
    target_root: Path
    merged_root: Path
    plex_mount_root: PurePath
    ignore_paths: tuple[Path, ...]
    managed_roots: tuple[str, ...]
    tautulli_url: str
    tautulli_api_key: str
    tautulli_lookback_days: int
    frequent_budget_bytes: int
    cache_min_free_space_bytes: int
    cache_target_free_space_bytes: int
    min_file_age_seconds: int
    loop_interval_seconds: int
    state_file: Path
    dry_run: bool


@dataclass(frozen=True)
class UnitStats:
    relative_dir: Path
    size_on_cache: int
    size_on_tank: int
    oldest_cache_mtime: float | None
    oldest_tank_mtime: float | None

    @property
    def total_size(self) -> int:
        return self.size_on_cache + self.size_on_tank


@dataclass(frozen=True)
class MoveResult:
    moved_bytes: int = 0
    conflicts: int = 0


@dataclass(frozen=True)
class SyncResult:
    synced_bytes: int = 0
    synced_units: int = 0
    replaced_units: int = 0
    skipped_units: int = 0
    conflicts: int = 0


@dataclass(frozen=True)
class EvictResult:
    evicted_units: int = 0
    evicted_bytes: int = 0
    conflicts: int = 0


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def require_mountpoint(path: Path, name: str) -> None:
    if not path.is_mount():
        raise SystemExit(f"{name} must be a mountpoint: {path}")


def parse_ignore_paths(source_root: Path, value: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw_item in value.split(":"):
        item = raw_item.strip()
        if not item:
            continue
        path = Path(item)
        try:
            path.relative_to(source_root)
        except ValueError as exc:
            raise SystemExit(f"ignore path must stay under source root: {path}") from exc
        paths.append(path)
    return tuple(paths)


def parse_managed_roots(value: str) -> tuple[str, ...]:
    roots = tuple(item.strip() for item in value.split(":") if item.strip())
    if not roots:
        raise SystemExit("managed roots must not be empty")
    for root in roots:
        if "/" in root:
            raise SystemExit(f"managed root must not contain '/': {root}")
    return roots


def parse_size(value: str) -> int:
    text = value.strip().upper()
    if not text:
        raise SystemExit("size value must not be empty")
    suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    multiplier = 1
    if text[-1] in suffixes:
        multiplier = suffixes[text[-1]]
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError as exc:
        raise SystemExit(f"invalid size value: {value}") from exc


def parse_duration(value: str) -> int:
    text = value.strip().lower()
    if not text:
        raise SystemExit("duration value must not be empty")
    units = {"s": 1, "m": 60, "h": 3600}
    multiplier = 1
    if text[-1] in units:
        multiplier = units[text[-1]]
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError as exc:
        raise SystemExit(f"invalid duration value: {value}") from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage archive sync, hot copies, and cache eviction.")
    parser.add_argument("--demote-non-frequent", action="store_true")
    parser.add_argument("--cache-target-free-space", metavar="SIZE")
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--promote-only", action="store_true")
    parser.add_argument("--evict-only", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--loop-interval", metavar="DURATION")
    return parser.parse_args(argv)


def load_config(args: argparse.Namespace) -> Config:
    override_active = args.cache_target_free_space is not None
    source_root = Path(require_env("SOURCE_DIR"))
    target_root = Path(require_env("TARGET_DIR"))
    merged_root = Path(require_env("MERGED_ROOT"))
    plex_mount_root = PurePath(require_env("PLEX_MOUNT_ROOT"))
    ignore_paths = parse_ignore_paths(source_root, os.environ.get("IGNORE_PATHS", ""))
    managed_roots = parse_managed_roots(require_env("MANAGED_ROOTS"))
    tautulli_url = require_env("TAUTULLI_URL").rstrip("/")
    tautulli_api_key = require_env("TAUTULLI_API_KEY").strip().strip('"')
    tautulli_lookback_days = int(require_env("TAUTULLI_LOOKBACK_DAYS"))
    frequent_budget_bytes = parse_size(require_env("FREQUENT_BUDGET"))
    cache_min_free_space_bytes = parse_size(require_env("CACHE_MIN_FREE_SPACE"))
    cache_target_free_space_bytes = parse_size(
        args.cache_target_free_space or require_env("CACHE_TARGET_FREE_SPACE")
    )
    min_file_age_seconds = parse_duration(os.environ.get("MIN_FILE_AGE", "5m"))
    loop_interval_seconds = parse_duration(args.loop_interval or os.environ.get("LOOP_INTERVAL", "5m"))
    state_file = Path(require_env("STATE_FILE"))
    dry_run = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}

    if not source_root.is_dir():
        raise SystemExit(f"source directory does not exist: {source_root}")
    if not target_root.is_dir():
        raise SystemExit(f"target directory does not exist: {target_root}")
    if not merged_root.is_dir():
        raise SystemExit(f"merged directory does not exist: {merged_root}")
    require_mountpoint(target_root, "TARGET_DIR")
    require_mountpoint(merged_root, "MERGED_ROOT")
    if tautulli_lookback_days < 1:
        raise SystemExit("TAUTULLI_LOOKBACK_DAYS must be at least 1")
    if frequent_budget_bytes < 1:
        raise SystemExit("FREQUENT_BUDGET must be positive")
    if cache_min_free_space_bytes < 1:
        raise SystemExit("CACHE_MIN_FREE_SPACE must be positive")
    if min_file_age_seconds < 0:
        raise SystemExit("MIN_FILE_AGE must not be negative")
    if loop_interval_seconds < 1:
        raise SystemExit("loop interval must be positive")
    if override_active and cache_target_free_space_bytes < cache_min_free_space_bytes:
        raise SystemExit("--cache-target-free-space must be at least CACHE_MIN_FREE_SPACE")
    if not override_active and cache_target_free_space_bytes <= cache_min_free_space_bytes:
        raise SystemExit("CACHE_TARGET_FREE_SPACE must be greater than CACHE_MIN_FREE_SPACE")

    return Config(
        source_root=source_root,
        target_root=target_root,
        merged_root=merged_root,
        plex_mount_root=plex_mount_root,
        ignore_paths=ignore_paths,
        managed_roots=managed_roots,
        tautulli_url=tautulli_url,
        tautulli_api_key=tautulli_api_key,
        tautulli_lookback_days=tautulli_lookback_days,
        frequent_budget_bytes=frequent_budget_bytes,
        cache_min_free_space_bytes=cache_min_free_space_bytes,
        cache_target_free_space_bytes=cache_target_free_space_bytes,
        min_file_age_seconds=min_file_age_seconds,
        loop_interval_seconds=loop_interval_seconds,
        state_file=state_file,
        dry_run=dry_run,
    )


def file_is_open(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["fuser", "-s", str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def is_ignored(path: Path, ignore_paths: tuple[Path, ...]) -> bool:
    return any(path == ignore_path or ignore_path in path.parents for ignore_path in ignore_paths)


def is_temp_name(name: str) -> bool:
    return name.endswith(IGNORED_SUFFIXES) or name.endswith(TEMP_SUFFIX)


def should_skip_scan(path: Path, ignore_paths: tuple[Path, ...]) -> bool:
    if path.is_symlink() or not path.is_file():
        return True
    if is_ignored(path, ignore_paths):
        return True
    if any(part.startswith(IGNORED_PREFIX) for part in path.parts):
        return True
    return is_temp_name(path.name)


def is_file_stable(path: Path, config: Config) -> bool:
    if should_skip_scan(path, config.ignore_paths):
        return False
    if file_is_open(path):
        return False
    return time.time() - path.stat().st_mtime >= config.min_file_age_seconds


def prune_empty_dirs(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir()]
    directories.sort(key=lambda path: (len(path.relative_to(root).parts), str(path)), reverse=True)
    for directory in directories:
        if len(directory.relative_to(root).parts) <= 1:
            continue
        try:
            directory.rmdir()
            print(f"removed empty directory: {directory}")
        except OSError:
            continue


def cleanup_stale_temp_files(root: Path) -> None:
    for path in root.rglob(f"*{TEMP_SUFFIX}"):
        if time.time() - path.stat().st_mtime < 86400:
            continue
        try:
            path.unlink()
            print(f"removed stale temp file: {path}")
        except OSError:
            continue


def read_state(path: Path) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {"units": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"units": {}}
    units = data.get("units")
    if not isinstance(units, dict):
        return {"units": {}}
    return {"units": units}


def write_state(path: Path, state: dict[str, dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tautulli_get(base_url: str, api_key: str, cmd: str, **params: object) -> dict[str, object]:
    query = {"apikey": api_key, "cmd": cmd}
    for key, value in params.items():
        if value is not None:
            query[key] = str(value)
    url = f"{base_url}/api/v2?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)
    response_payload = payload.get("response", {})
    if response_payload.get("result") != "success":
        raise RuntimeError(
            f"Tautulli API call failed for {cmd}: {response_payload.get('message') or 'unknown error'}"
        )
    return response_payload.get("data", {})


def history_rows(base_url: str, api_key: str, after_date: str, media_type: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    start = 0
    page_size = 1000
    while True:
        data = tautulli_get(
            base_url,
            api_key,
            "get_history",
            after=after_date,
            media_type=media_type,
            length=page_size,
            start=start,
        )
        page_rows = data.get("data", [])
        if not isinstance(page_rows, list):
            break
        rows.extend(row for row in page_rows if isinstance(row, dict))
        if len(page_rows) < page_size:
            break
        start += page_size
    return rows


def score_row(row: dict[str, object]) -> int:
    for key in ("play_duration", "duration"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return max(int(value), 1)
            except (TypeError, ValueError):
                pass
    try:
        return max(int(row.get("stopped")) - int(row.get("started")) - int(row.get("paused_counter", 0)), 1)
    except (TypeError, ValueError):
        return 1


def parse_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_unit_from_rating_key(config: Config, api_key: str, rating_key: int, root_name: str) -> Path | None:
    metadata = tautulli_get(config.tautulli_url, api_key, "get_metadata", rating_key=rating_key)
    media_info = metadata.get("media_info", [])
    if not isinstance(media_info, list):
        return None
    for media_item in media_info:
        if not isinstance(media_item, dict):
            continue
        for part in media_item.get("parts", []):
            if not isinstance(part, dict):
                continue
            raw_file = part.get("file")
            if not isinstance(raw_file, str) or not raw_file:
                continue
            relative_dir = unit_relative_dir_from_plex_path(config, raw_file, root_name)
            if relative_dir is not None:
                return relative_dir
    return None


def build_hot_scores(config: Config) -> dict[Path, int]:
    after_date = time.strftime(
        "%Y-%m-%d",
        time.localtime(time.time() - (config.tautulli_lookback_days * 86400)),
    )
    scores: dict[Path, int] = {}
    season_samples: dict[int, int] = {}
    season_scores: dict[int, int] = {}

    for row in history_rows(config.tautulli_url, config.tautulli_api_key, after_date, "movie"):
        rating_key = parse_int(row.get("rating_key"))
        if rating_key is None:
            continue
        sample = resolve_unit_from_rating_key(config, config.tautulli_api_key, rating_key, "movies")
        if sample is None:
            sample = resolve_unit_from_rating_key(config, config.tautulli_api_key, rating_key, "movies4k")
        if sample is None:
            continue
        scores[sample] = scores.get(sample, 0) + score_row(row)

    for row in history_rows(config.tautulli_url, config.tautulli_api_key, after_date, "episode"):
        parent_rating_key = parse_int(row.get("parent_rating_key"))
        rating_key = parse_int(row.get("rating_key"))
        if parent_rating_key is None or rating_key is None:
            continue
        season_scores[parent_rating_key] = season_scores.get(parent_rating_key, 0) + score_row(row)
        season_samples.setdefault(parent_rating_key, rating_key)

    for season_key, rating_key in season_samples.items():
        sample = resolve_unit_from_rating_key(config, config.tautulli_api_key, rating_key, "tv")
        if sample is None:
            sample = resolve_unit_from_rating_key(config, config.tautulli_api_key, rating_key, "tv4k")
        if sample is None:
            continue
        scores[sample] = scores.get(sample, 0) + season_scores.get(season_key, 0)

    return scores


def try_build_hot_scores(config: Config) -> tuple[dict[Path, int], bool]:
    try:
        return build_hot_scores(config), True
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"warning: Tautulli unavailable, skipping hot-cache actions: {exc}")
        return {}, False


def unit_relative_dir_from_plex_path(config: Config, plex_path: str, root_name: str) -> Path | None:
    raw_path = PurePath(plex_path)
    try:
        plex_relative = raw_path.relative_to(config.plex_mount_root)
    except ValueError:
        return None
    if not plex_relative.parts or plex_relative.parts[0] != root_name:
        return None
    if root_name in MOVIE_ROOTS and len(plex_relative.parts) >= 2:
        return Path(*plex_relative.parts[:2])
    if root_name in TV_ROOTS and len(plex_relative.parts) >= 3:
        return Path(*plex_relative.parts[:3])
    return None


def unit_relative_dir_from_relative_path(relative: Path) -> Path | None:
    parts = relative.parts
    if not parts:
        return None
    if parts[0] in MOVIE_ROOTS and len(parts) >= 2:
        return Path(parts[0]) / parts[1]
    if parts[0] in TV_ROOTS and len(parts) >= 3:
        return Path(parts[0]) / parts[1] / parts[2]
    return None


def collect_units(root: Path, config: Config) -> dict[Path, tuple[int, float | None]]:
    units: dict[Path, tuple[int, float | None]] = {}
    for managed_root in config.managed_roots:
        managed_path = root / managed_root
        if not managed_path.exists():
            continue
        for path in managed_path.rglob("*"):
            if should_skip_scan(path, config.ignore_paths):
                continue
            relative = path.relative_to(root)
            unit = unit_relative_dir_from_relative_path(relative)
            if unit is None:
                continue
            stat_result = path.stat()
            current_size, current_oldest = units.get(unit, (0, None))
            oldest = stat_result.st_mtime if current_oldest is None else min(current_oldest, stat_result.st_mtime)
            units[unit] = (current_size + stat_result.st_size, oldest)
    return units


def collect_all_unit_stats(config: Config) -> dict[Path, UnitStats]:
    cache_units = collect_units(config.source_root, config)
    target_units = collect_units(config.target_root, config)
    stats: dict[Path, UnitStats] = {}
    for unit in set(cache_units) | set(target_units):
        cache_size, cache_oldest = cache_units.get(unit, (0, None))
        target_size, target_oldest = target_units.get(unit, (0, None))
        stats[unit] = UnitStats(unit, cache_size, target_size, cache_oldest, target_oldest)
    return stats


def unit_age_key(relative_dir: Path, stats: UnitStats | None, state: dict[str, dict[str, float]]) -> tuple[float, str]:
    if stats is not None and stats.oldest_cache_mtime is not None:
        return (stats.oldest_cache_mtime, str(relative_dir))
    return (float(state.get("units", {}).get(str(relative_dir), {}).get("first_seen", 0.0)), str(relative_dir))


def select_desired_frequent_units(unit_stats: dict[Path, UnitStats], hot_scores: dict[Path, int], budget_bytes: int) -> set[Path]:
    desired: set[Path] = set()
    used = 0
    for relative_dir, _score in sorted(hot_scores.items(), key=lambda item: (-item[1], str(item[0]))):
        stats = unit_stats.get(relative_dir)
        if stats is None or stats.total_size < 1:
            continue
        if desired and used + stats.total_size > budget_bytes:
            continue
        desired.add(relative_dir)
        used += stats.total_size
        if used >= budget_bytes:
            break
    return desired


def relative_file_map(root: Path, relative_dir: Path, config: Config, stable_only: bool) -> dict[Path, Path]:
    base = root / relative_dir
    if not base.exists():
        return {}
    files: dict[Path, Path] = {}
    for path in sorted(base.rglob("*")):
        if should_skip_scan(path, config.ignore_paths):
            continue
        if stable_only and not is_file_stable(path, config):
            continue
        files[path.relative_to(base)] = path
    return files


def ensure_target_parent_dirs(source: Path, source_root: Path, target_root: Path) -> None:
    relative_parent = source.relative_to(source_root).parent
    current_source = source_root
    current_target = target_root
    for part in relative_parent.parts:
        current_source = current_source / part
        current_target = current_target / part
        if current_target.exists():
            continue
        current_target.mkdir()
        source_stat = current_source.stat()
        os.chown(current_target, source_stat.st_uid, source_stat.st_gid)
        os.chmod(current_target, stat.S_IMODE(source_stat.st_mode))


def copy_file(source: Path, source_root: Path, target_root: Path) -> int:
    relative_path = source.relative_to(source_root)
    target = target_root / relative_path
    ensure_target_parent_dirs(source, source_root, target_root)
    source_stat = source.stat()

    if target.exists():
        if source.samefile(target):
            return 0
        target_stat = target.stat()
        if source_stat.st_size == target_stat.st_size and int(source_stat.st_mtime) == int(target_stat.st_mtime):
            return 0
        if source_stat.st_size == target_stat.st_size and file_hash(source) == file_hash(target):
            os.utime(target, (source_stat.st_atime, source_stat.st_mtime))
            return 0

    temp_target = target.with_name(f".{target.name}{TEMP_SUFFIX}")
    if temp_target.exists():
        temp_target.unlink()
    shutil.copy2(source, temp_target)
    os.chown(temp_target, source_stat.st_uid, source_stat.st_gid)
    os.replace(temp_target, target)
    print(f"copied: {source} -> {target}")
    return source_stat.st_size


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sync_directory(relative_dir: Path, config: Config) -> MoveResult:
    source_dir = config.source_root / relative_dir
    target_dir = config.target_root / relative_dir
    stable_source_files = relative_file_map(config.source_root, relative_dir, config, stable_only=True)
    if not stable_source_files:
        return MoveResult()
    ensure_target_parent_dirs(source_dir / next(iter(stable_source_files)), config.source_root, config.target_root)
    moved_bytes = 0
    for relative_file, source_path in stable_source_files.items():
        moved_bytes += copy_file(source_path, config.source_root, config.target_root)
    target_files = relative_file_map(config.target_root, relative_dir, config, stable_only=False)
    stale_files = [target_dir / relative_file for relative_file in target_files if relative_file not in stable_source_files]
    for stale_path in stale_files:
        if stale_path.exists():
            stale_path.unlink()
            print(f"removed stale archive file: {stale_path}")
    prune_empty_dirs(target_dir)
    replaced = 1 if stale_files else 0
    return MoveResult(moved_bytes=moved_bytes, conflicts=replaced)


def file_group_key(relative_dir: Path, file_name: str) -> str | None:
    stem = file_name.split(".", 1)[0]
    if relative_dir.parts[0] in MOVIE_ROOTS:
        return relative_dir.parts[1]
    match = EPISODE_RE.match(stem)
    if match is None:
        return None
    season = match.group("season")
    first = int(match.group("first"))
    second_text = match.group("second")
    episodes = [first]
    if second_text is not None:
        second = int(second_text)
        if second >= first:
            episodes.extend(range(first + 1, second + 1))
        else:
            episodes.append(second)
    return f"{relative_dir.parts[1]}|S{season}|{'-'.join(f'{episode:02d}' for episode in episodes)}"


def group_relative_files(relative_dir: Path, files: dict[Path, Path]) -> dict[str, dict[Path, Path]]:
    groups: dict[str, dict[Path, Path]] = {}
    for relative_file, full_path in files.items():
        key = file_group_key(relative_dir, relative_file.name)
        if key is None:
            continue
        groups.setdefault(key, {})[relative_file] = full_path
    return groups


def sync_tv_unit(relative_dir: Path, config: Config) -> MoveResult:
    source_files = relative_file_map(config.source_root, relative_dir, config, stable_only=True)
    if not source_files:
        return MoveResult()
    target_dir = config.target_root / relative_dir
    source_groups = group_relative_files(relative_dir, source_files)
    target_groups = group_relative_files(relative_dir, relative_file_map(config.target_root, relative_dir, config, stable_only=False))
    moved_bytes = 0
    replaced_groups = 0
    for key, files in source_groups.items():
        existing = target_groups.get(key, {})
        stale_paths = [target_dir / rel for rel in existing if rel not in files]
        if stale_paths:
            replaced_groups += 1
        for stale_path in stale_paths:
            if stale_path.exists():
                stale_path.unlink()
                print(f"removed stale archive file: {stale_path}")
        for relative_file, source_path in files.items():
            moved_bytes += copy_file(source_path, config.source_root, config.target_root)
    prune_empty_dirs(target_dir)
    return MoveResult(moved_bytes=moved_bytes, conflicts=replaced_groups)


def sync_archive(config: Config) -> SyncResult:
    synced_bytes = 0
    synced_units = 0
    replaced_units = 0
    skipped_units = 0
    conflicts = 0
    unit_stats = collect_all_unit_stats(config)
    for relative_dir, stats in sorted(unit_stats.items(), key=lambda item: str(item[0])):
        if stats.size_on_cache < 1:
            continue
        source_dir = config.source_root / relative_dir
        if not source_dir.exists():
            continue
        if relative_dir.parts[0] in MOVIE_ROOTS:
            source_files = relative_file_map(config.source_root, relative_dir, config, stable_only=False)
            if not source_files or any(not is_file_stable(path, config) for path in source_files.values()):
                skipped_units += 1
                continue
            result = sync_directory(relative_dir, config)
        else:
            all_files = relative_file_map(config.source_root, relative_dir, config, stable_only=False)
            if not all_files:
                continue
            stable_files = relative_file_map(config.source_root, relative_dir, config, stable_only=True)
            if not stable_files:
                skipped_units += 1
                continue
            result = sync_tv_unit(relative_dir, config)
        if result.moved_bytes > 0 or result.conflicts > 0:
            synced_units += 1
            synced_bytes += result.moved_bytes
            replaced_units += result.conflicts
    return SyncResult(synced_bytes, synced_units, replaced_units, skipped_units, conflicts)


def remove_cache_file(path: Path, source_root: Path) -> int:
    size = path.stat().st_size
    path.unlink()
    print(f"evicted cache file: {path}")
    prune_empty_dirs(path.parent)
    return size


def archive_current_for_unit(relative_dir: Path, config: Config) -> bool:
    source_files = relative_file_map(config.source_root, relative_dir, config, stable_only=False)
    target_files = relative_file_map(config.target_root, relative_dir, config, stable_only=False)
    if not source_files:
        return True
    if relative_dir.parts[0] in MOVIE_ROOTS:
        return set(source_files) == set(target_files)
    source_groups = group_relative_files(relative_dir, source_files)
    target_groups = group_relative_files(relative_dir, target_files)
    return set(source_groups) <= set(target_groups)


def evict_unit(relative_dir: Path, config: Config) -> MoveResult:
    source_dir = config.source_root / relative_dir
    if not source_dir.exists() or not archive_current_for_unit(relative_dir, config):
        return MoveResult(conflicts=1)
    moved_bytes = 0
    for path in sorted(source_dir.rglob("*")):
        if should_skip_scan(path, config.ignore_paths):
            continue
        if not is_file_stable(path, config):
            continue
        moved_bytes += remove_cache_file(path, config.source_root)
    prune_empty_dirs(source_dir)
    return MoveResult(moved_bytes=moved_bytes)


def promote_unit(relative_dir: Path, config: Config) -> MoveResult:
    target_dir = config.target_root / relative_dir
    if not target_dir.exists():
        return MoveResult()
    moved_bytes = 0
    for path in sorted(target_dir.rglob("*")):
        if should_skip_scan(path, ()):
            continue
        moved_bytes += copy_file(path, config.target_root, config.source_root)
    return MoveResult(moved_bytes=moved_bytes)


def update_state(state: dict[str, dict[str, float]], cache_units: dict[Path, int], desired_frequent: set[Path]) -> None:
    now = time.time()
    unit_state = state.setdefault("units", {})
    active_keys = {str(unit) for unit, size in cache_units.items() if size > 0 and unit not in desired_frequent}
    for key in active_keys:
        entry = unit_state.get(key)
        if not isinstance(entry, dict):
            unit_state[key] = {"first_seen": now}
        else:
            entry.setdefault("first_seen", now)
    for key in [key for key in unit_state if key not in active_keys]:
        unit_state.pop(key, None)


def filesystem_usage(path: Path) -> tuple[int, int, int]:
    statvfs = os.statvfs(path)
    total = statvfs.f_frsize * statvfs.f_blocks
    available = statvfs.f_frsize * statvfs.f_bavail
    return total, total - available, available


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value}B"
    units = ["KiB", "MiB", "GiB", "TiB", "PiB"]
    size = float(value)
    for unit in units:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.1f}{unit}"
    return f"{size:.1f}EiB"


def try_acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def run_once(config: Config, args: argparse.Namespace) -> int:
    state = read_state(config.state_file)
    cleanup_stale_temp_files(config.source_root)
    cleanup_stale_temp_files(config.target_root)

    sync_result = SyncResult()
    promote_result = MoveResult()
    evict_recent = EvictResult()
    evict_frequent = EvictResult()
    conflict_count = 0

    unit_stats = collect_all_unit_stats(config)
    hot_scores, tautulli_available = try_build_hot_scores(config)
    desired_frequent = select_desired_frequent_units(unit_stats, hot_scores, config.frequent_budget_bytes)

    do_sync = not args.promote_only and not args.evict_only
    do_promote = not args.sync_only and not args.evict_only
    do_evict = args.demote_non_frequent or (not args.sync_only and not args.promote_only)

    if not tautulli_available:
        if do_promote:
            print("warning: skipping promotion because Tautulli is unavailable")
        if do_evict:
            print("warning: skipping eviction because Tautulli is unavailable")
        do_promote = False
        do_evict = False

    if do_sync:
        sync_result = sync_archive(config)
        conflict_count += sync_result.conflicts
        unit_stats = collect_all_unit_stats(config)

    if do_promote:
        for relative_dir in sorted(desired_frequent, key=str):
            stats = unit_stats.get(relative_dir)
            if stats is None or stats.size_on_tank < 1 or stats.size_on_cache > 0:
                continue
            result = promote_unit(relative_dir, config) if not config.dry_run else MoveResult(moved_bytes=stats.size_on_tank)
            conflict_count += result.conflicts
            if result.moved_bytes > 0:
                promote_result = MoveResult(promote_result.moved_bytes + result.moved_bytes, promote_result.conflicts)
        if not config.dry_run:
            unit_stats = collect_all_unit_stats(config)

    _total_bytes, used_bytes, available_bytes = filesystem_usage(config.source_root)
    should_evict = do_evict and (args.demote_non_frequent or available_bytes < config.cache_min_free_space_bytes)

    if args.demote_non_frequent:
        print("manual mode: demoting non-frequent cached media regardless of free-space threshold")
    if should_evict:
        recent_candidates = sorted(
            (unit for unit, stats in unit_stats.items() if stats.size_on_cache > 0 and unit not in desired_frequent),
            key=lambda unit: unit_age_key(unit, unit_stats.get(unit), state),
        )
        for relative_dir in recent_candidates:
            if not args.demote_non_frequent and available_bytes >= config.cache_target_free_space_bytes:
                break
            result = evict_unit(relative_dir, config) if not config.dry_run else MoveResult(moved_bytes=unit_stats[relative_dir].size_on_cache)
            conflict_count += result.conflicts
            if result.moved_bytes > 0:
                evict_recent = EvictResult(evict_recent.evicted_units + 1, evict_recent.evicted_bytes + result.moved_bytes, evict_recent.conflicts)
                available_bytes += result.moved_bytes
                used_bytes -= result.moved_bytes

        if available_bytes < config.cache_target_free_space_bytes:
            frequent_candidates = sorted(
                (unit for unit in desired_frequent if unit_stats.get(unit) is not None and unit_stats[unit].size_on_cache > 0),
                key=lambda unit: (hot_scores.get(unit, 0), *unit_age_key(unit, unit_stats.get(unit), state)),
            )
            for relative_dir in frequent_candidates:
                if available_bytes >= config.cache_target_free_space_bytes:
                    break
                result = evict_unit(relative_dir, config) if not config.dry_run else MoveResult(moved_bytes=unit_stats[relative_dir].size_on_cache)
                conflict_count += result.conflicts
                if result.moved_bytes > 0:
                    evict_frequent = EvictResult(evict_frequent.evicted_units + 1, evict_frequent.evicted_bytes + result.moved_bytes, evict_frequent.conflicts)
                    available_bytes += result.moved_bytes
                    used_bytes -= result.moved_bytes

    if not config.dry_run:
        unit_stats = collect_all_unit_stats(config)
        cache_units = {unit: stats.size_on_cache for unit, stats in unit_stats.items() if stats.size_on_cache > 0}
        update_state(state, cache_units, desired_frequent)
        write_state(config.state_file, state)
        prune_empty_dirs(config.source_root)
        _total_bytes, used_bytes, available_bytes = filesystem_usage(config.source_root)

    print(
        f"{'dry-run: ' if config.dry_run else 'complete: '}"
        f"synced_units={sync_result.synced_units} synced_bytes={format_bytes(sync_result.synced_bytes)} "
        f"replaced_archive_units={sync_result.replaced_units} "
        f"skipped_unstable_units={sync_result.skipped_units} "
        f"promoted_units={1 if promote_result.moved_bytes > 0 else 0 if promote_result.moved_bytes == 0 else 0} "
        f"promoted_bytes={format_bytes(promote_result.moved_bytes)} "
        f"evicted_non_frequent_units={evict_recent.evicted_units} "
        f"evicted_non_frequent_bytes={format_bytes(evict_recent.evicted_bytes)} "
        f"evicted_frequent_units={evict_frequent.evicted_units} "
        f"evicted_frequent_bytes={format_bytes(evict_frequent.evicted_bytes)} "
        f"conflicts={conflict_count} "
        f"cache_used={format_bytes(used_bytes)} cache_avail={format_bytes(available_bytes)}"
    )
    return 0


def main() -> int:
    args = parse_args(sys.argv[1:])
    config = load_config(args)
    operations_lock_handle = try_acquire_lock(OPERATIONS_LOCK)
    if operations_lock_handle is None:
        print("skipped: shared media operations lock is held")
        return 0
    lock_handle = try_acquire_lock(config.state_file.parent / "run.lock")
    if lock_handle is None:
        operations_lock_handle.close()
        print("skipped: another homelab media mover instance is already running")
        return 0
    try:
        if not args.loop:
            return run_once(config, args)
        while True:
            exit_code = run_once(config, args)
            if exit_code != 0:
                return exit_code
            time.sleep(config.loop_interval_seconds)
    finally:
        lock_handle.close()
        operations_lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
