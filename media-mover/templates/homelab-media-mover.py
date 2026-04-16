#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePath

IGNORED_PREFIX = "."
IGNORED_SUFFIXES = (".part", ".tmp", ".partial", ".!qB")
MOVIE_ROOT = "movies"
TV_ROOT = "tv"


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


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


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
        raise SystemExit("FREQUENT_BUDGET must not be empty")
    suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    multiplier = 1
    if text[-1] in suffixes:
        multiplier = suffixes[text[-1]]
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError as exc:
        raise SystemExit(f"invalid size value: {value}") from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move media between cache and archive tiers.")
    parser.add_argument(
        "--demote-non-frequent",
        action="store_true",
        help="demote non-frequent cached media even when cache free space is above the minimum",
    )
    parser.add_argument(
        "--cache-target-free-space",
        metavar="SIZE",
        help="override CACHE_TARGET_FREE_SPACE for this run",
    )
    return parser.parse_args(argv)


def load_config(*, cache_target_free_space_override: str | None = None) -> Config:
    override_active = cache_target_free_space_override is not None
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
        cache_target_free_space_override
        if cache_target_free_space_override is not None
        else require_env("CACHE_TARGET_FREE_SPACE")
    )
    state_file = Path(require_env("STATE_FILE"))
    dry_run = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}

    if not source_root.is_dir():
        raise SystemExit(f"source directory does not exist: {source_root}")
    if not target_root.is_dir():
        raise SystemExit(f"target directory does not exist: {target_root}")
    if not merged_root.is_dir():
        raise SystemExit(f"merged directory does not exist: {merged_root}")
    if tautulli_lookback_days < 1:
        raise SystemExit("TAUTULLI_LOOKBACK_DAYS must be at least 1")
    if frequent_budget_bytes < 1:
        raise SystemExit("FREQUENT_BUDGET must be positive")
    if cache_min_free_space_bytes < 1:
        raise SystemExit("CACHE_MIN_FREE_SPACE must be positive")
    if override_active and cache_target_free_space_bytes < cache_min_free_space_bytes:
        raise SystemExit(
            "--cache-target-free-space must be at least CACHE_MIN_FREE_SPACE"
        )
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


def should_skip(path: Path, ignore_paths: tuple[Path, ...]) -> bool:
    if path.is_symlink() or not path.is_file():
        return True
    if is_ignored(path, ignore_paths):
        return True
    if any(part.startswith(IGNORED_PREFIX) for part in path.parts):
        return True
    if path.name.endswith(IGNORED_SUFFIXES):
        return True
    return file_is_open(path)


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
        if value is None:
            continue
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
        if value in (None, ""):
            continue
        try:
            return max(int(value), 1)
        except (TypeError, ValueError):
            continue
    started = row.get("started")
    stopped = row.get("stopped")
    paused_counter = row.get("paused_counter", 0)
    try:
        return max(int(stopped) - int(started) - int(paused_counter), 1)
    except (TypeError, ValueError):
        return 1


def build_hot_scores(config: Config) -> dict[Path, int]:
    api_key = config.tautulli_api_key
    after_date = time.strftime(
        "%Y-%m-%d",
        time.localtime(time.time() - (config.tautulli_lookback_days * 86400)),
    )
    scores: dict[Path, int] = {}
    sample_rating_keys: dict[Path, int] = {}

    for row in history_rows(config.tautulli_url, api_key, after_date, "movie"):
        rating_key = parse_int(row.get("rating_key"))
        if rating_key is None:
            continue
        sample = resolve_unit_from_rating_key(config, api_key, rating_key, MOVIE_ROOT)
        if sample is None:
            continue
        scores[sample] = scores.get(sample, 0) + score_row(row)
        sample_rating_keys.setdefault(sample, rating_key)

    season_samples: dict[int, int] = {}
    season_scores: dict[int, int] = {}
    for row in history_rows(config.tautulli_url, api_key, after_date, "episode"):
        parent_rating_key = parse_int(row.get("parent_rating_key"))
        rating_key = parse_int(row.get("rating_key"))
        if parent_rating_key is None or rating_key is None:
            continue
        season_scores[parent_rating_key] = season_scores.get(parent_rating_key, 0) + score_row(row)
        season_samples.setdefault(parent_rating_key, rating_key)

    for season_key, rating_key in season_samples.items():
        sample = resolve_unit_from_rating_key(config, api_key, rating_key, TV_ROOT)
        if sample is None:
            continue
        scores[sample] = scores.get(sample, 0) + season_scores.get(season_key, 0)

    return scores


def parse_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_unit_from_rating_key(
    config: Config,
    api_key: str,
    rating_key: int,
    root_name: str,
) -> Path | None:
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


def unit_relative_dir_from_plex_path(config: Config, plex_path: str, root_name: str) -> Path | None:
    raw_path = PurePath(plex_path)
    try:
        plex_relative = raw_path.relative_to(config.plex_mount_root)
    except ValueError:
        return None
    if not plex_relative.parts or plex_relative.parts[0] != root_name:
        return None
    if root_name == MOVIE_ROOT:
        if len(plex_relative.parts) < 2:
            return None
        return Path(*plex_relative.parts[:2])
    if root_name == TV_ROOT:
        if len(plex_relative.parts) < 3:
            return None
        return Path(*plex_relative.parts[:3])
    return None


def collect_units(root: Path, config: Config) -> dict[Path, tuple[int, float | None]]:
    units: dict[Path, tuple[int, float | None]] = {}
    for managed_root in config.managed_roots:
        managed_path = root / managed_root
        if not managed_path.exists():
            continue
        for path in managed_path.rglob("*"):
            if should_skip(path, config.ignore_paths):
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            unit = unit_relative_dir_from_relative_path(relative)
            if unit is None:
                continue
            stat_result = path.stat()
            current_size, current_oldest = units.get(unit, (0, None))
            oldest_mtime = stat_result.st_mtime if current_oldest is None else min(current_oldest, stat_result.st_mtime)
            units[unit] = (current_size + stat_result.st_size, oldest_mtime)
    return units


def unit_relative_dir_from_relative_path(relative: Path) -> Path | None:
    parts = relative.parts
    if not parts:
        return None
    if parts[0] == MOVIE_ROOT:
        if len(parts) < 2:
            return None
        return Path(parts[0]) / parts[1]
    if parts[0] == TV_ROOT:
        if len(parts) < 3:
            return None
        return Path(parts[0]) / parts[1] / parts[2]
    return None


def collect_all_unit_stats(config: Config) -> dict[Path, UnitStats]:
    cache_units = collect_units(config.source_root, config)
    tank_units = collect_units(config.target_root, config)
    all_units = set(cache_units) | set(tank_units)
    stats: dict[Path, UnitStats] = {}
    for unit in all_units:
        cache_size, cache_oldest = cache_units.get(unit, (0, None))
        tank_size, tank_oldest = tank_units.get(unit, (0, None))
        stats[unit] = UnitStats(
            relative_dir=unit,
            size_on_cache=cache_size,
            size_on_tank=tank_size,
            oldest_cache_mtime=cache_oldest,
            oldest_tank_mtime=tank_oldest,
        )
    return stats


def unit_age_key(relative_dir: Path, stats: UnitStats | None, state: dict[str, dict[str, float]]) -> tuple[float, str]:
    if stats is not None and stats.oldest_cache_mtime is not None:
        return (stats.oldest_cache_mtime, str(relative_dir))
    return (
        float(state.get("units", {}).get(str(relative_dir), {}).get("first_seen", 0.0)),
        str(relative_dir),
    )


def select_desired_frequent_units(
    unit_stats: dict[Path, UnitStats],
    hot_scores: dict[Path, int],
    budget_bytes: int,
) -> set[Path]:
    desired: set[Path] = set()
    used = 0
    ranked_units = sorted(hot_scores.items(), key=lambda item: (-item[1], str(item[0])))
    for relative_dir, _score in ranked_units:
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


def move_file(source: Path, source_root: Path, target_root: Path) -> int:
    relative_path = source.relative_to(source_root)
    target = target_root / relative_path
    ensure_target_parent_dirs(source, source_root, target_root)

    if target.exists():
        if source.samefile(target):
            raise RuntimeError(f"target resolves to the source file itself: {target}")
        source_stat = source.stat()
        target_stat = target.stat()
        if (
            source_stat.st_size == target_stat.st_size
            and int(source_stat.st_mtime) == int(target_stat.st_mtime)
        ):
            source.unlink()
            print(f"removed duplicate source file: {source}")
            return 0
        if source_stat.st_size == target_stat.st_size and file_hash(source) == file_hash(target):
            source.unlink()
            print(f"removed duplicate source file after hash check: {source}")
            return 0
        raise RuntimeError(f"target already exists with different content: {target}")

    size = source.stat().st_size
    temp_target = target.with_name(f".{target.name}.homelab-media-mover.tmp")
    if temp_target.exists():
        temp_target.unlink()

    try:
        shutil.copy2(source, temp_target)
        stat_result = source.stat()
        os.chown(temp_target, stat_result.st_uid, stat_result.st_gid)
        os.replace(temp_target, target)
    except Exception:
        try:
            if temp_target.exists():
                temp_target.unlink()
        except OSError:
            pass
        raise

    source.unlink()
    print(f"moved: {source} -> {target}")
    return size


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


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def dry_run_move_unit(
    relative_dir: Path,
    source_root: Path,
    target_root: Path,
    ignore_paths: tuple[Path, ...],
) -> MoveResult:
    source_dir = source_root / relative_dir
    if not source_dir.exists():
        return MoveResult()
    moved_bytes = 0
    target_dir = target_root / relative_dir
    for path in sorted(source_dir.rglob("*")):
        if should_skip(path, ignore_paths):
            continue
        moved_bytes += path.stat().st_size
        print(f"would move: {path} -> {target_dir / path.relative_to(source_dir)}")
    return MoveResult(moved_bytes=moved_bytes)


def move_unit(relative_dir: Path, source_root: Path, target_root: Path, ignore_paths: tuple[Path, ...]) -> MoveResult:
    source_dir = source_root / relative_dir
    if not source_dir.exists():
        return MoveResult()
    moved_bytes = 0
    conflicts = 0
    for path in sorted(source_dir.rglob("*")):
        if should_skip(path, ignore_paths):
            continue
        try:
            moved_bytes += move_file(path, source_root, target_root)
        except RuntimeError as exc:
            conflicts += 1
            print(f"skipped conflict: {exc}")
    prune_empty_dirs(source_dir)
    return MoveResult(moved_bytes=moved_bytes, conflicts=conflicts)


def update_state(
    state: dict[str, dict[str, float]],
    cache_units: dict[Path, int],
    desired_frequent: set[Path],
) -> None:
    now = time.time()
    unit_state = state.setdefault("units", {})
    active_keys = {str(unit) for unit, size in cache_units.items() if size > 0 and unit not in desired_frequent}
    for key in active_keys:
        entry = unit_state.get(key)
        if not isinstance(entry, dict):
            unit_state[key] = {"first_seen": now}
            continue
        entry.setdefault("first_seen", now)
    stale_keys = [key for key in unit_state if key not in active_keys]
    for key in stale_keys:
        unit_state.pop(key, None)


def filesystem_usage(path: Path) -> tuple[int, int, int]:
    statvfs = os.statvfs(path)
    total = statvfs.f_frsize * statvfs.f_blocks
    available = statvfs.f_frsize * statvfs.f_bavail
    used = total - available
    return total, used, available


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


def main() -> int:
    args = parse_args(sys.argv[1:])
    config = load_config(cache_target_free_space_override=args.cache_target_free_space)
    state = read_state(config.state_file)

    hot_scores = build_hot_scores(config)
    unit_stats = collect_all_unit_stats(config)
    desired_frequent = select_desired_frequent_units(
        unit_stats,
        hot_scores,
        config.frequent_budget_bytes,
    )

    promoted_units = 0
    promoted_bytes = 0
    conflict_count = 0
    for relative_dir in sorted(desired_frequent, key=str):
        stats = unit_stats.get(relative_dir)
        if stats is None or stats.size_on_tank < 1:
            continue
        result = (
            dry_run_move_unit(relative_dir, config.target_root, config.source_root, ())
            if config.dry_run
            else move_unit(relative_dir, config.target_root, config.source_root, ())
        )
        conflict_count += result.conflicts
        if result.moved_bytes > 0:
            promoted_units += 1
            promoted_bytes += result.moved_bytes

    if not config.dry_run:
        unit_stats = collect_all_unit_stats(config)
        cache_units = {unit: stats.size_on_cache for unit, stats in unit_stats.items() if stats.size_on_cache > 0}
        update_state(state, cache_units, desired_frequent)

    _total_bytes, used_bytes, available_bytes = filesystem_usage(config.source_root)
    should_demote_recent = args.demote_non_frequent or available_bytes < config.cache_min_free_space_bytes

    demoted_recent_units = 0
    demoted_recent_bytes = 0
    demoted_frequent_units = 0
    demoted_frequent_bytes = 0
    if args.demote_non_frequent:
        print("manual mode: demoting non-frequent cached media regardless of free-space threshold")
    if should_demote_recent:
        recent_candidates = sorted(
            (
                unit
                for unit, stats in unit_stats.items()
                if stats.size_on_cache > 0 and unit not in desired_frequent
            ),
            key=lambda unit: unit_age_key(unit, unit_stats.get(unit), state),
        )
        for relative_dir in recent_candidates:
            if not args.demote_non_frequent and available_bytes >= config.cache_target_free_space_bytes:
                break
            result = (
                dry_run_move_unit(relative_dir, config.source_root, config.target_root, config.ignore_paths)
                if config.dry_run
                else move_unit(relative_dir, config.source_root, config.target_root, config.ignore_paths)
            )
            conflict_count += result.conflicts
            if result.moved_bytes > 0:
                demoted_recent_units += 1
                demoted_recent_bytes += result.moved_bytes
                used_bytes -= result.moved_bytes
                available_bytes += result.moved_bytes

        if available_bytes < config.cache_target_free_space_bytes:
            frequent_candidates = sorted(
                (
                    unit
                    for unit in desired_frequent
                    if unit_stats.get(unit) is not None and unit_stats[unit].size_on_cache > 0
                ),
                key=lambda unit: (
                    hot_scores.get(unit, 0),
                    *unit_age_key(unit, unit_stats.get(unit), state),
                ),
            )
            for relative_dir in frequent_candidates:
                if available_bytes >= config.cache_target_free_space_bytes:
                    break
                result = (
                    dry_run_move_unit(relative_dir, config.source_root, config.target_root, config.ignore_paths)
                    if config.dry_run
                    else move_unit(relative_dir, config.source_root, config.target_root, config.ignore_paths)
                )
                conflict_count += result.conflicts
                if result.moved_bytes > 0:
                    demoted_frequent_units += 1
                    demoted_frequent_bytes += result.moved_bytes
                    used_bytes -= result.moved_bytes
                    available_bytes += result.moved_bytes

    if not config.dry_run:
        unit_stats = collect_all_unit_stats(config)
        cache_units = {unit: stats.size_on_cache for unit, stats in unit_stats.items() if stats.size_on_cache > 0}
        update_state(state, cache_units, desired_frequent)
        write_state(config.state_file, state)
        prune_empty_dirs(config.source_root)
        total_bytes, used_bytes, available_bytes = filesystem_usage(config.source_root)
    print(
        f"{'dry-run: ' if config.dry_run else 'complete: '}"
        f"promoted_units={promoted_units} promoted_bytes={format_bytes(promoted_bytes)} "
        f"demoted_recent_units={demoted_recent_units} "
        f"demoted_recent_bytes={format_bytes(demoted_recent_bytes)} "
        f"demoted_frequent_units={demoted_frequent_units} "
        f"demoted_frequent_bytes={format_bytes(demoted_frequent_bytes)} "
        f"conflicts={conflict_count} "
        f"cache_used={format_bytes(used_bytes)} cache_avail={format_bytes(available_bytes)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
