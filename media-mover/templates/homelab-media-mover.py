#!/usr/bin/env python3

from __future__ import annotations

import argparse
import configparser
import fcntl
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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePath

IGNORED_PREFIX = "."
IGNORED_SUFFIXES = (".part", ".tmp", ".partial", ".!qB")
TEMP_SUFFIX = ".homelab-media-mover.tmp"
MOVIE_ROOTS = {"movies", "movies4k"}
TV_ROOTS = {"tv", "tv4k"}
TV_UNIT_KEY_PART = "__episodes__"
MOVIE_FOLDER_RE = re.compile(r"^.+ \((?P<year>\d{4})\) \{tmdb-(?P<tmdb>\d+)\}$")
EPISODE_RE = re.compile(r"^(?P<title>.+?) - S(?P<season>\d{2})E(?P<first>\d{2})(?:(?:E|-)(?P<second>\d{2}))?$")
OPERATIONS_LOCK = Path("/var/lib/homelab-media/operations.lock")


@dataclass(frozen=True)
class Config:
    source_root: Path
    target_root: Path
    merged_root: Path
    plex_mount_root: PurePath
    plex_url: str
    tautulli_config_path: Path
    ignore_paths: tuple[Path, ...]
    managed_roots: tuple[str, ...]
    tautulli_url: str
    tautulli_api_key: str
    tautulli_lookback_days: int
    frequent_budget_bytes: int
    ondeck_enabled: bool
    ondeck_budget_bytes: int
    ondeck_tv_prefetch_episodes: int
    ondeck_include_movies: bool
    ondeck_movie_max_age_days: int
    ondeck_series_max_age_days: int
    watchlist_enabled: bool
    watchlist_budget_bytes: int
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


@dataclass(frozen=True)
class PlexContext:
    server_url: str
    admin_token: str
    user_tokens: dict[str, str]


@dataclass(frozen=True)
class CacheEffectivenessStats:
    watched_units: int = 0
    watched_score: int = 0
    watched_cached_units: int = 0
    watched_cached_score: int = 0


@dataclass(frozen=True)
class OnDeckEntry:
    relative_dir: Path
    score: int
    item_type: str
    age_days: float | None
    progress_percent: float | None
    current: bool


@dataclass(frozen=True)
class OnDeckAgeStats:
    current_movies: int = 0
    current_episodes: int = 0
    movie_median_age_days: float | None = None
    episode_median_age_days: float | None = None
    movie_over_age_limit: int = 0
    episode_over_age_limit: int = 0


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    parser.add_argument("--wait-for-lock", action="store_true")
    parser.add_argument("--loop-interval", metavar="DURATION")
    parser.add_argument("--report-cache-effectiveness", action="store_true")
    parser.add_argument("--report-limit", type=int, default=10, metavar="N")
    return parser.parse_args(argv)


def load_config(args: argparse.Namespace) -> Config:
    override_active = args.cache_target_free_space is not None
    source_root = Path(require_env("SOURCE_DIR"))
    target_root = Path(require_env("TARGET_DIR"))
    merged_root = Path(require_env("MERGED_ROOT"))
    plex_mount_root = PurePath(require_env("PLEX_MOUNT_ROOT"))
    plex_url = require_env("PLEX_URL").rstrip("/")
    tautulli_config_path = Path(require_env("TAUTULLI_CONFIG_PATH"))
    ignore_paths = parse_ignore_paths(source_root, os.environ.get("IGNORE_PATHS", ""))
    managed_roots = parse_managed_roots(require_env("MANAGED_ROOTS"))
    tautulli_url = require_env("TAUTULLI_URL").rstrip("/")
    tautulli_api_key = require_env("TAUTULLI_API_KEY").strip().strip('"')
    tautulli_lookback_days = int(require_env("TAUTULLI_LOOKBACK_DAYS"))
    frequent_budget_bytes = parse_size(require_env("FREQUENT_BUDGET"))
    ondeck_enabled = parse_bool(os.environ.get("ONDECK_ENABLED", "false"))
    ondeck_budget_bytes = parse_size(require_env("ONDECK_BUDGET"))
    ondeck_tv_prefetch_episodes = int(require_env("ONDECK_TV_PREFETCH_EPISODES"))
    ondeck_include_movies = parse_bool(os.environ.get("ONDECK_INCLUDE_MOVIES", "false"))
    ondeck_movie_max_age_days = int(require_env("ONDECK_MOVIE_MAX_AGE_DAYS"))
    ondeck_series_max_age_days = int(require_env("ONDECK_SERIES_MAX_AGE_DAYS"))
    watchlist_enabled = parse_bool(os.environ.get("WATCHLIST_ENABLED", "false"))
    watchlist_budget_bytes = parse_size(require_env("WATCHLIST_BUDGET"))
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
    if ondeck_budget_bytes < 1:
        raise SystemExit("ONDECK_BUDGET must be positive")
    if ondeck_tv_prefetch_episodes < 0:
        raise SystemExit("ONDECK_TV_PREFETCH_EPISODES must not be negative")
    if ondeck_movie_max_age_days < 1:
        raise SystemExit("ONDECK_MOVIE_MAX_AGE_DAYS must be positive")
    if ondeck_series_max_age_days < 1:
        raise SystemExit("ONDECK_SERIES_MAX_AGE_DAYS must be positive")
    if watchlist_budget_bytes < 1:
        raise SystemExit("WATCHLIST_BUDGET must be positive")
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
        plex_url=plex_url,
        tautulli_config_path=tautulli_config_path,
        ignore_paths=ignore_paths,
        managed_roots=managed_roots,
        tautulli_url=tautulli_url,
        tautulli_api_key=tautulli_api_key,
        tautulli_lookback_days=tautulli_lookback_days,
        frequent_budget_bytes=frequent_budget_bytes,
        ondeck_enabled=ondeck_enabled,
        ondeck_budget_bytes=ondeck_budget_bytes,
        ondeck_tv_prefetch_episodes=ondeck_tv_prefetch_episodes,
        ondeck_include_movies=ondeck_include_movies,
        ondeck_movie_max_age_days=ondeck_movie_max_age_days,
        ondeck_series_max_age_days=ondeck_series_max_age_days,
        watchlist_enabled=watchlist_enabled,
        watchlist_budget_bytes=watchlist_budget_bytes,
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


def item_age_days(item: ET.Element) -> float | None:
    last_viewed_at = parse_int(item.get("lastViewedAt"))
    if last_viewed_at is None:
        return None
    return max(time.time() - last_viewed_at, 0) / 86400.0


def item_progress_percent(item: ET.Element) -> float | None:
    duration = parse_int(item.get("duration"))
    if duration is None or duration < 1:
        return None
    view_offset = parse_int(item.get("viewOffset")) or 0
    return max(min((view_offset / duration) * 100.0, 100.0), 0.0)


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def resolve_unit_from_rating_key(
    config: Config,
    rating_key: int,
    resolved_units: dict[int, Path | None],
) -> Path | None:
    if rating_key in resolved_units:
        return resolved_units[rating_key]

    try:
        metadata = tautulli_get(
            config.tautulli_url,
            config.tautulli_api_key,
            "get_metadata",
            rating_key=rating_key,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"warning: skipping Tautulli metadata for rating_key={rating_key}: {exc}")
        resolved_units[rating_key] = None
        return None

    media_info = metadata.get("media_info", [])
    if not isinstance(media_info, list):
        resolved_units[rating_key] = None
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
            relative_dir = unit_relative_dir_from_plex_path(config, raw_file)
            if relative_dir is not None:
                resolved_units[rating_key] = relative_dir
                return relative_dir

    resolved_units[rating_key] = None
    return None


def build_hot_scores(config: Config) -> dict[Path, int]:
    after_date = time.strftime(
        "%Y-%m-%d",
        time.localtime(time.time() - (config.tautulli_lookback_days * 86400)),
    )
    scores: dict[Path, int] = {}
    resolved_units: dict[int, Path | None] = {}

    for row in history_rows(config.tautulli_url, config.tautulli_api_key, after_date, "movie"):
        rating_key = parse_int(row.get("rating_key"))
        if rating_key is None:
            continue
        sample = resolve_unit_from_rating_key(config, rating_key, resolved_units)
        if sample is None:
            continue
        scores[sample] = scores.get(sample, 0) + score_row(row)

    for row in history_rows(config.tautulli_url, config.tautulli_api_key, after_date, "episode"):
        rating_key = parse_int(row.get("rating_key"))
        if rating_key is None:
            continue
        sample = resolve_unit_from_rating_key(config, rating_key, resolved_units)
        if sample is None:
            continue
        scores[sample] = scores.get(sample, 0) + score_row(row)

    return scores


def plex_get_xml(url: str, token: str) -> ET.Element:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/xml",
            "X-Plex-Token": token,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return ET.fromstring(response.read())


def plex_get_json(url: str, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Plex-Token": token,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected Plex payload type for {url}")
    return payload


def plex_part_paths(video: ET.Element) -> list[str]:
    return [
        path
        for path in (
            part.get("file", "").strip()
            for part in video.findall(".//Part")
        )
        if path
    ]


def unit_set_from_plex_paths(config: Config, paths: list[str]) -> set[Path]:
    units: set[Path] = set()
    for raw_path in paths:
        unit = unit_relative_dir_from_plex_path(config, raw_path)
        if unit is not None:
            units.add(unit)
    return units


def load_plex_context(config: Config) -> PlexContext:
    parser = configparser.ConfigParser()
    read_paths = parser.read(config.tautulli_config_path, encoding="utf-8")
    if not read_paths:
        raise RuntimeError(f"missing Tautulli config for Plex token lookup: {config.tautulli_config_path}")
    if not parser.has_section("PMS"):
        raise RuntimeError(f"missing PMS section in Tautulli config: {config.tautulli_config_path}")
    admin_token = parser.get("PMS", "pms_token", fallback="").strip()
    if not admin_token:
        raise RuntimeError(f"missing pms_token in Tautulli config: {config.tautulli_config_path}")
    machine_identifier = parser.get("PMS", "pms_identifier", fallback="").strip()

    admin_user = plex_get_json("https://plex.tv/api/v2/user", admin_token)
    admin_name = str(admin_user.get("username") or admin_user.get("title") or "admin")
    user_tokens = {admin_name: admin_token}

    if machine_identifier:
        shared_root = plex_get_xml(
            f"https://plex.tv/api/servers/{machine_identifier}/shared_servers",
            admin_token,
        )
        for item in shared_root.findall("SharedServer"):
            token = item.get("accessToken", "").strip()
            username = (
                item.get("username", "").strip()
                or item.get("name", "").strip()
                or item.get("id", "").strip()
            )
            if token and username:
                user_tokens[username] = token

    return PlexContext(server_url=config.plex_url, admin_token=admin_token, user_tokens=user_tokens)


def collect_ondeck_entries(config: Config) -> list[OnDeckEntry]:
    if not config.ondeck_enabled:
        return []

    context = load_plex_context(config)
    entries: list[OnDeckEntry] = []
    show_cache: dict[tuple[str, str], list[tuple[tuple[int, int], set[Path]]]] = {}

    for username, token in sorted(context.user_tokens.items()):
        try:
            root = plex_get_xml(f"{context.server_url}/library/onDeck", token)
        except (OSError, urllib.error.URLError, ET.ParseError) as exc:
            print(f"warning: skipping Plex On Deck for {username}: {exc}")
            continue

        for item in list(root):
            item_type = item.get("type", "").strip().lower()
            current_units = unit_set_from_plex_paths(config, plex_part_paths(item))
            age_days = item_age_days(item)
            progress_percent = item_progress_percent(item)
            if item_type == "movie":
                if not config.ondeck_include_movies:
                    continue
                for unit in current_units:
                    entries.append(OnDeckEntry(unit, 100, item_type, age_days, progress_percent, True))
                continue
            if item_type != "episode":
                continue

            for unit in current_units:
                entries.append(OnDeckEntry(unit, 100, item_type, age_days, progress_percent, True))

            if config.ondeck_tv_prefetch_episodes < 1:
                continue

            show_key = item.get("grandparentRatingKey", "").strip()
            season_index = parse_int(item.get("parentIndex"))
            episode_index = parse_int(item.get("index"))
            if not show_key or season_index is None or episode_index is None:
                continue

            cache_key = (token, show_key)
            if cache_key not in show_cache:
                leaves_root = plex_get_xml(
                    f"{context.server_url}/library/metadata/{show_key}/allLeaves",
                    token,
                )
                episodes: list[tuple[tuple[int, int], set[Path]]] = []
                for episode in leaves_root.findall("Video"):
                    episode_season = parse_int(episode.get("parentIndex"))
                    episode_number = parse_int(episode.get("index"))
                    if episode_season is None or episode_number is None:
                        continue
                    units = unit_set_from_plex_paths(config, plex_part_paths(episode))
                    if not units:
                        continue
                    episodes.append(((episode_season, episode_number), units))
                episodes.sort(key=lambda item: item[0])
                show_cache[cache_key] = episodes

            next_episodes = 0
            current_key = (season_index, episode_index)
            for episode_key, units in show_cache[cache_key]:
                if episode_key <= current_key:
                    continue
                for unit in units:
                    entries.append(OnDeckEntry(unit, 10, item_type, age_days, progress_percent, False))
                next_episodes += 1
                if next_episodes >= config.ondeck_tv_prefetch_episodes:
                    break

    return entries


def build_ondeck_scores_from_entries(entries: list[OnDeckEntry], config: Config) -> dict[Path, int]:
    scores: dict[Path, int] = {}
    for entry in entries:
        if (
            entry.item_type == "movie"
            and entry.age_days is not None
            and entry.age_days > config.ondeck_movie_max_age_days
        ):
            continue
        if (
            entry.item_type == "episode"
            and entry.age_days is not None
            and entry.age_days > config.ondeck_series_max_age_days
        ):
            continue
        scores[entry.relative_dir] = scores.get(entry.relative_dir, 0) + entry.score
    return scores


def build_ondeck_scores(config: Config) -> dict[Path, int]:
    return build_ondeck_scores_from_entries(collect_ondeck_entries(config), config)


def build_ondeck_age_stats(entries: list[OnDeckEntry], config: Config) -> OnDeckAgeStats:
    current_movies = [entry for entry in entries if entry.current and entry.item_type == "movie"]
    current_episodes = [entry for entry in entries if entry.current and entry.item_type == "episode"]
    movie_ages = [entry.age_days for entry in current_movies if entry.age_days is not None]
    episode_ages = [entry.age_days for entry in current_episodes if entry.age_days is not None]
    return OnDeckAgeStats(
        current_movies=len(current_movies),
        current_episodes=len(current_episodes),
        movie_median_age_days=median(movie_ages),
        episode_median_age_days=median(episode_ages),
        movie_over_age_limit=sum(1 for age in movie_ages if age > config.ondeck_movie_max_age_days),
        episode_over_age_limit=sum(1 for age in episode_ages if age > config.ondeck_series_max_age_days),
    )


def resolve_unit_from_plex_rating_key(
    config: Config,
    context: PlexContext,
    rating_key: int,
    cache: dict[int, Path | None],
) -> Path | None:
    if rating_key in cache:
        return cache[rating_key]

    root = plex_get_xml(f"{context.server_url}/library/metadata/{rating_key}", context.admin_token)
    for item in list(root):
        units = unit_set_from_plex_paths(config, plex_part_paths(item))
        if units:
            unit = sorted(units, key=str)[0]
            cache[rating_key] = unit
            return unit

    cache[rating_key] = None
    return None


def resolve_watchlist_show_unit(
    config: Config,
    context: PlexContext,
    rating_key: int,
    cache: dict[int, Path | None],
) -> Path | None:
    if rating_key in cache:
        return cache[rating_key]

    root = plex_get_xml(f"{context.server_url}/library/metadata/{rating_key}/allLeaves", context.admin_token)
    unwatched: list[tuple[tuple[int, int], Path]] = []
    first_seen: list[tuple[tuple[int, int], Path]] = []
    for episode in root.findall("Video"):
        season_index = parse_int(episode.get("parentIndex"))
        episode_index = parse_int(episode.get("index"))
        if season_index is None:
            continue
        if episode_index is None:
            continue
        units = unit_set_from_plex_paths(config, plex_part_paths(episode))
        if not units:
            continue
        unit = sorted(units, key=str)[0]
        episode_key = (season_index, episode_index)
        first_seen.append((episode_key, unit))
        if parse_int(episode.get("viewCount")) in (None, 0):
            unwatched.append((episode_key, unit))

    if unwatched:
        selected = min(unwatched)[1]
        cache[rating_key] = selected
        return selected
    if first_seen:
        selected = min(first_seen)[1]
        cache[rating_key] = selected
        return selected

    cache[rating_key] = None
    return None


def search_local_items_by_guid(config: Config, context: PlexContext, guid: str) -> list[ET.Element]:
    guid_plain = guid.split("?", 1)[0].strip()
    if not guid_plain:
        return []
    root = plex_get_xml(
        f"{context.server_url}/library/all?{urllib.parse.urlencode({'guid': guid_plain})}",
        context.admin_token,
    )
    return list(root)


def collect_admin_watchlist(context: PlexContext) -> list[ET.Element]:
    items: list[ET.Element] = []
    start = 0
    page_size = 100
    while True:
        request = urllib.request.Request(
            "https://discover.provider.plex.tv/library/sections/watchlist/all",
            headers={
                "Accept": "application/xml",
                "X-Plex-Token": context.admin_token,
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(page_size),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(response.read())
        page = list(root)
        items.extend(page)
        total_size = parse_int(root.get("totalSize")) or parse_int(root.get("size")) or 0
        start += len(page)
        if not page or start >= total_size:
            break
    return items


def build_watchlist_scores(config: Config) -> dict[Path, int]:
    if not config.watchlist_enabled:
        return {}

    context = load_plex_context(config)
    media_cache: dict[int, Path | None] = {}
    show_cache: dict[int, Path | None] = {}
    scores: dict[Path, int] = {}

    items = collect_admin_watchlist(context)
    if items:
        print("warning: direct Plex watchlist currently uses the main Plex account only")
    total_items = len(items)
    for index, item in enumerate(items):
        media_type = item.get("type", "").strip().lower()
        guid = item.get("guid", "").strip()
        if media_type not in {"movie", "show"} or not guid:
            continue
        matches = search_local_items_by_guid(config, context, guid)
        if not matches:
            continue
        if media_type == "movie":
            rating_key = parse_int(matches[0].get("ratingKey"))
            if rating_key is None:
                continue
            unit = resolve_unit_from_plex_rating_key(config, context, rating_key, media_cache)
        else:
            rating_key = parse_int(matches[0].get("ratingKey"))
            if rating_key is None:
                continue
            unit = resolve_watchlist_show_unit(config, context, rating_key, show_cache)
        if unit is None:
            continue
        scores[unit] = scores.get(unit, 0) + max(total_items - index, 1)

    return scores


def try_build_hot_scores(config: Config) -> tuple[dict[Path, int], bool]:
    try:
        return build_hot_scores(config), True
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"warning: Tautulli unavailable, skipping hot-cache actions: {exc}")
        return {}, False


def try_build_ondeck_scores(config: Config) -> dict[Path, int]:
    try:
        return build_ondeck_scores(config)
    except (
        configparser.Error,
        OSError,
        RuntimeError,
        ValueError,
        ET.ParseError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as exc:
        print(f"warning: skipping Plex On Deck cache actions: {exc}")
        return {}


def try_collect_ondeck_entries(config: Config) -> list[OnDeckEntry]:
    try:
        return collect_ondeck_entries(config)
    except (
        configparser.Error,
        OSError,
        RuntimeError,
        ValueError,
        ET.ParseError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as exc:
        print(f"warning: skipping Plex On Deck cache actions: {exc}")
        return []


def try_build_watchlist_scores(config: Config) -> dict[Path, int]:
    try:
        return build_watchlist_scores(config)
    except (
        configparser.Error,
        OSError,
        RuntimeError,
        ValueError,
        ET.ParseError,
        urllib.error.URLError,
    ) as exc:
        print(f"warning: skipping Plex watchlist cache actions: {exc}")
        return {}


def unit_relative_dir_from_plex_path(config: Config, plex_path: str) -> Path | None:
    raw_path = PurePath(plex_path)
    try:
        plex_relative = raw_path.relative_to(config.plex_mount_root)
    except ValueError:
        return None
    if not plex_relative.parts:
        return None
    root_name = plex_relative.parts[0]
    if root_name not in config.managed_roots:
        return None
    if root_name in MOVIE_ROOTS and len(plex_relative.parts) >= 2:
        return Path(*plex_relative.parts[:2])
    if root_name in TV_ROOTS and len(plex_relative.parts) >= 4:
        season_dir = Path(*plex_relative.parts[:3])
        key = file_group_key(season_dir, plex_relative.parts[3])
        if key is not None:
            return tv_unit_from_group_key(season_dir, key)
    return None


def unit_relative_dir_from_relative_path(relative: Path) -> Path | None:
    parts = relative.parts
    if not parts:
        return None
    if parts[0] in MOVIE_ROOTS and len(parts) >= 2:
        return Path(parts[0]) / parts[1]
    if parts[0] in TV_ROOTS and len(parts) >= 4:
        season_dir = Path(*parts[:3])
        key = file_group_key(season_dir, parts[3])
        if key is not None:
            return tv_unit_from_group_key(season_dir, key)
    return None


def is_tv_episode_unit(relative_dir: Path) -> bool:
    return len(relative_dir.parts) >= 5 and relative_dir.parts[0] in TV_ROOTS and relative_dir.parts[3] == TV_UNIT_KEY_PART


def tv_unit_from_group_key(season_dir: Path, key: str) -> Path:
    return season_dir / TV_UNIT_KEY_PART / key


def tv_unit_parent_dir(relative_dir: Path) -> Path:
    return Path(*relative_dir.parts[:3])


def tv_unit_group_key(relative_dir: Path) -> str:
    return relative_dir.parts[4]


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


def select_desired_units(unit_stats: dict[Path, UnitStats], scores: dict[Path, int], budget_bytes: int) -> set[Path]:
    desired: set[Path] = set()
    used = 0
    for relative_dir, _score in sorted(scores.items(), key=lambda item: (-item[1], str(item[0]))):
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


def select_desired_frequent_units(unit_stats: dict[Path, UnitStats], hot_scores: dict[Path, int], budget_bytes: int) -> set[Path]:
    return select_desired_units(unit_stats, hot_scores, budget_bytes)


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


def tv_unit_file_map(root: Path, relative_dir: Path, config: Config, stable_only: bool) -> dict[Path, Path]:
    if not is_tv_episode_unit(relative_dir):
        return {}
    season_dir = tv_unit_parent_dir(relative_dir)
    base = root / season_dir
    if not base.exists():
        return {}
    group_key = tv_unit_group_key(relative_dir)
    files: dict[Path, Path] = {}
    for path in sorted(base.rglob("*")):
        if should_skip_scan(path, config.ignore_paths):
            continue
        if stable_only and not is_file_stable(path, config):
            continue
        if file_group_key(season_dir, path.name) != group_key:
            continue
        files[path.relative_to(base)] = path
    return files


def unit_file_map(root: Path, relative_dir: Path, config: Config, stable_only: bool) -> dict[Path, Path]:
    if is_tv_episode_unit(relative_dir):
        return tv_unit_file_map(root, relative_dir, config, stable_only)
    return relative_file_map(root, relative_dir, config, stable_only)


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
        if source_stat.st_size == target_stat.st_size:
            return 0

    temp_target = target.with_name(f".{target.name}{TEMP_SUFFIX}")
    if temp_target.exists():
        temp_target.unlink()
    shutil.copy2(source, temp_target)
    os.chown(temp_target, source_stat.st_uid, source_stat.st_gid)
    os.replace(temp_target, target)
    print(f"copied: {source} -> {target}")
    return source_stat.st_size


def sync_directory(relative_dir: Path, config: Config) -> MoveResult:
    source_dir = config.source_root / relative_dir
    stable_source_files = relative_file_map(config.source_root, relative_dir, config, stable_only=True)
    if not stable_source_files:
        return MoveResult()
    ensure_target_parent_dirs(source_dir / next(iter(stable_source_files)), config.source_root, config.target_root)
    moved_bytes = 0
    for relative_file, source_path in stable_source_files.items():
        moved_bytes += copy_file(source_path, config.source_root, config.target_root)
    return MoveResult(moved_bytes=moved_bytes)


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
    source_files = unit_file_map(config.source_root, relative_dir, config, stable_only=True)
    if not source_files:
        return MoveResult()
    moved_bytes = 0
    for source_path in source_files.values():
        moved_bytes += copy_file(source_path, config.source_root, config.target_root)
    return MoveResult(moved_bytes=moved_bytes)


def sync_archive(config: Config, inline_evict_non_frequent: set[Path] | None = None) -> tuple[SyncResult, EvictResult]:
    synced_bytes = 0
    synced_units = 0
    replaced_units = 0
    skipped_units = 0
    conflicts = 0
    evicted_units = 0
    evicted_bytes = 0
    evict_conflicts = 0
    unit_stats = collect_all_unit_stats(config)
    for relative_dir, stats in sorted(unit_stats.items(), key=lambda item: str(item[0])):
        if stats.size_on_cache < 1:
            continue
        if relative_dir.parts[0] in MOVIE_ROOTS:
            source_files = unit_file_map(config.source_root, relative_dir, config, stable_only=False)
            if not source_files or any(not is_file_stable(path, config) for path in source_files.values()):
                skipped_units += 1
                continue
            result = sync_directory(relative_dir, config)
        else:
            all_files = unit_file_map(config.source_root, relative_dir, config, stable_only=False)
            if not all_files:
                continue
            stable_files = unit_file_map(config.source_root, relative_dir, config, stable_only=True)
            if not stable_files:
                skipped_units += 1
                continue
            result = sync_tv_unit(relative_dir, config)
        if result.moved_bytes > 0 or result.conflicts > 0:
            synced_units += 1
            synced_bytes += result.moved_bytes
            replaced_units += result.conflicts
            if inline_evict_non_frequent is None or relative_dir not in inline_evict_non_frequent:
                continue
            evict_result = evict_unit(relative_dir, config) if not config.dry_run else MoveResult(moved_bytes=stats.size_on_cache)
            evict_conflicts += evict_result.conflicts
            if evict_result.moved_bytes > 0:
                evicted_units += 1
                evicted_bytes += evict_result.moved_bytes
    return (
        SyncResult(synced_bytes, synced_units, replaced_units, skipped_units, conflicts),
        EvictResult(evicted_units, evicted_bytes, evict_conflicts),
    )


def remove_cache_file(path: Path, source_root: Path) -> int:
    size = path.stat().st_size
    path.unlink()
    print(f"evicted cache file: {path}")
    prune_empty_dirs(path.parent)
    return size


def archive_current_for_unit(relative_dir: Path, config: Config) -> bool:
    source_files = unit_file_map(config.source_root, relative_dir, config, stable_only=False)
    target_files = unit_file_map(config.target_root, relative_dir, config, stable_only=False)
    if not source_files:
        return True
    return set(source_files) == set(target_files)


def evict_unit(relative_dir: Path, config: Config) -> MoveResult:
    source_files = unit_file_map(config.source_root, relative_dir, config, stable_only=True)
    if not source_files or not archive_current_for_unit(relative_dir, config):
        return MoveResult(conflicts=1)
    moved_bytes = 0
    for path in source_files.values():
        moved_bytes += remove_cache_file(path, config.source_root)
    prune_empty_dirs(config.source_root / (tv_unit_parent_dir(relative_dir) if is_tv_episode_unit(relative_dir) else relative_dir))
    return MoveResult(moved_bytes=moved_bytes)


def promote_unit(relative_dir: Path, config: Config) -> MoveResult:
    target_files = unit_file_map(config.target_root, relative_dir, config, stable_only=False)
    if not target_files:
        return MoveResult()
    moved_bytes = 0
    for path in target_files.values():
        moved_bytes += copy_file(path, config.target_root, config.source_root)
    return MoveResult(moved_bytes=moved_bytes)


def update_state(state: dict[str, dict[str, float]], cache_units: dict[Path, int], protected_units: set[Path]) -> None:
    now = time.time()
    unit_state = state.setdefault("units", {})
    active_keys = {str(unit) for unit, size in cache_units.items() if size > 0 and unit not in protected_units}
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


def format_percent(numerator: int, denominator: int) -> str:
    if denominator < 1:
        return "n/a"
    return f"{(numerator / denominator) * 100:.1f}%"


def format_days(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def build_cache_effectiveness_stats(
    unit_stats: dict[Path, UnitStats],
    hot_scores: dict[Path, int],
) -> CacheEffectivenessStats:
    watched_cached_units = 0
    watched_cached_score = 0
    for relative_dir, score in hot_scores.items():
        stats = unit_stats.get(relative_dir)
        if stats is None or stats.size_on_cache < 1:
            continue
        watched_cached_units += 1
        watched_cached_score += score
    return CacheEffectivenessStats(
        watched_units=len(hot_scores),
        watched_score=sum(hot_scores.values()),
        watched_cached_units=watched_cached_units,
        watched_cached_score=watched_cached_score,
    )


def report_cache_effectiveness(config: Config, args: argparse.Namespace) -> int:
    if args.report_limit < 1:
        raise SystemExit("--report-limit must be positive")

    unit_stats = collect_all_unit_stats(config)
    hot_scores, tautulli_available = try_build_hot_scores(config)
    ondeck_entries = try_collect_ondeck_entries(config)
    ondeck_scores = build_ondeck_scores_from_entries(ondeck_entries, config)
    ondeck_age_stats = build_ondeck_age_stats(ondeck_entries, config)
    watchlist_scores = try_build_watchlist_scores(config)
    desired_frequent = select_desired_frequent_units(unit_stats, hot_scores, config.frequent_budget_bytes)
    desired_ondeck = select_desired_units(unit_stats, ondeck_scores, config.ondeck_budget_bytes)
    desired_watchlist = select_desired_units(unit_stats, watchlist_scores, config.watchlist_budget_bytes)
    effectiveness = build_cache_effectiveness_stats(unit_stats, hot_scores)
    cache_units = {unit: stats for unit, stats in unit_stats.items() if stats.size_on_cache > 0}
    _total_bytes, used_bytes, available_bytes = filesystem_usage(config.source_root)

    def cached_count(units: set[Path]) -> int:
        return sum(1 for unit in units if unit_stats.get(unit) is not None and unit_stats[unit].size_on_cache > 0)

    print(
        f"report: tautulli_available={'true' if tautulli_available else 'false'} "
        f"lookback_days={config.tautulli_lookback_days} "
        f"watched_units={effectiveness.watched_units} "
        f"watched_score={effectiveness.watched_score} "
        f"watched_cached_units={effectiveness.watched_cached_units} "
        f"watched_cached_score={effectiveness.watched_cached_score} "
        f"watched_unit_hit_rate={format_percent(effectiveness.watched_cached_units, effectiveness.watched_units)} "
        f"watched_weighted_hit_rate={format_percent(effectiveness.watched_cached_score, effectiveness.watched_score)} "
        f"desired_frequent_units={len(desired_frequent)} desired_frequent_cached_units={cached_count(desired_frequent)} "
        f"desired_ondeck_units={len(desired_ondeck)} desired_ondeck_cached_units={cached_count(desired_ondeck)} "
        f"desired_watchlist_units={len(desired_watchlist)} desired_watchlist_cached_units={cached_count(desired_watchlist)} "
        f"cache_units={len(cache_units)} cache_used={format_bytes(used_bytes)} cache_avail={format_bytes(available_bytes)}"
    )
    print(
        f"ondeck_age: movie_age_limit_days={config.ondeck_movie_max_age_days} "
        f"series_age_limit_days={config.ondeck_series_max_age_days} "
        f"current_movies={ondeck_age_stats.current_movies} current_episodes={ondeck_age_stats.current_episodes} "
        f"movie_median_age_days={format_days(ondeck_age_stats.movie_median_age_days)} "
        f"episode_median_age_days={format_days(ondeck_age_stats.episode_median_age_days)} "
        f"movie_over_age_limit={ondeck_age_stats.movie_over_age_limit} "
        f"episode_over_age_limit={ondeck_age_stats.episode_over_age_limit}"
    )

    if not hot_scores:
        print("top_watched: none")
        return 0

    for index, (relative_dir, score) in enumerate(
        sorted(hot_scores.items(), key=lambda item: (-item[1], str(item[0])))[: args.report_limit],
        start=1,
    ):
        stats = unit_stats.get(relative_dir)
        if stats is None:
            location = "missing"
            total_size = 0
        elif stats.size_on_cache > 0:
            location = "cache"
            total_size = stats.total_size
        elif stats.size_on_tank > 0:
            location = "archive"
            total_size = stats.total_size
        else:
            location = "missing"
            total_size = 0
        print(
            f"top_watched: rank={index} location={location} score={score} size={format_bytes(total_size)} unit={relative_dir}"
        )
    return 0


def try_acquire_lock(path: Path, *, wait: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        lock_mode = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), lock_mode)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def evict_units(
    candidates: list[Path],
    unit_stats: dict[Path, UnitStats],
    config: Config,
    available_bytes: int,
    stop_at_target: bool,
) -> tuple[EvictResult, int, int, int]:
    result_total = EvictResult()
    used_delta = 0
    conflict_count = 0
    for relative_dir in candidates:
        if stop_at_target and available_bytes >= config.cache_target_free_space_bytes:
            break
        result = evict_unit(relative_dir, config) if not config.dry_run else MoveResult(moved_bytes=unit_stats[relative_dir].size_on_cache)
        conflict_count += result.conflicts
        if result.moved_bytes > 0:
            result_total = EvictResult(
                result_total.evicted_units + 1,
                result_total.evicted_bytes + result.moved_bytes,
                result_total.conflicts,
            )
            available_bytes += result.moved_bytes
            used_delta += result.moved_bytes
    return result_total, available_bytes, used_delta, conflict_count


def run_once(config: Config, args: argparse.Namespace) -> int:
    state = read_state(config.state_file)
    cleanup_stale_temp_files(config.source_root)
    cleanup_stale_temp_files(config.target_root)

    sync_result = SyncResult()
    promote_result = MoveResult()
    promoted_units = 0
    evict_recent = EvictResult()
    evict_watchlist = EvictResult()
    evict_frequent = EvictResult()
    evict_ondeck = EvictResult()
    conflict_count = 0

    unit_stats = collect_all_unit_stats(config)
    hot_scores, tautulli_available = try_build_hot_scores(config)
    ondeck_scores = try_build_ondeck_scores(config)
    watchlist_scores = try_build_watchlist_scores(config)
    desired_frequent = select_desired_frequent_units(unit_stats, hot_scores, config.frequent_budget_bytes)
    desired_ondeck = select_desired_units(unit_stats, ondeck_scores, config.ondeck_budget_bytes)
    desired_watchlist = select_desired_units(unit_stats, watchlist_scores, config.watchlist_budget_bytes)
    protected_units = desired_frequent | desired_ondeck | desired_watchlist

    do_sync = not args.promote_only and not args.evict_only
    do_promote = not args.sync_only and not args.evict_only
    do_evict = args.demote_non_frequent or (not args.sync_only and not args.promote_only)
    if not tautulli_available:
        print("warning: Tautulli unavailable, frequent cache scoring is disabled for this run")

    _total_bytes, used_bytes, available_bytes = filesystem_usage(config.source_root)

    if args.demote_non_frequent and do_evict:
        print("manual mode: demoting non-frequent cached media regardless of free-space threshold")
        recent_candidates = sorted(
            (unit for unit, stats in unit_stats.items() if stats.size_on_cache > 0 and unit not in protected_units),
            key=lambda unit: unit_age_key(unit, unit_stats.get(unit), state),
        )
        pre_sync_recent, available_bytes, used_delta, pre_sync_conflicts = evict_units(
            recent_candidates,
            unit_stats,
            config,
            available_bytes,
            stop_at_target=False,
        )
        evict_recent = EvictResult(
            evict_recent.evicted_units + pre_sync_recent.evicted_units,
            evict_recent.evicted_bytes + pre_sync_recent.evicted_bytes,
            evict_recent.conflicts,
        )
        used_bytes -= used_delta
        conflict_count += pre_sync_conflicts
        if pre_sync_recent.evicted_units > 0 and not config.dry_run:
            unit_stats = collect_all_unit_stats(config)
    elif do_evict and available_bytes < config.cache_min_free_space_bytes:
        recent_candidates = sorted(
            (unit for unit, stats in unit_stats.items() if stats.size_on_cache > 0 and unit not in protected_units),
            key=lambda unit: unit_age_key(unit, unit_stats.get(unit), state),
        )
        pre_sync_recent, available_bytes, used_delta, pre_sync_conflicts = evict_units(
            recent_candidates,
            unit_stats,
            config,
            available_bytes,
            stop_at_target=True,
        )
        evict_recent = EvictResult(
            evict_recent.evicted_units + pre_sync_recent.evicted_units,
            evict_recent.evicted_bytes + pre_sync_recent.evicted_bytes,
            evict_recent.conflicts,
        )
        used_bytes -= used_delta
        conflict_count += pre_sync_conflicts
        if pre_sync_recent.evicted_units > 0 and not config.dry_run:
            unit_stats = collect_all_unit_stats(config)

    if do_sync:
        inline_evict_non_frequent = {unit for unit, stats in unit_stats.items() if stats.size_on_cache > 0 and unit not in protected_units} if args.demote_non_frequent and do_evict else None
        sync_result, inline_evict_recent = sync_archive(config, inline_evict_non_frequent)
        conflict_count += sync_result.conflicts
        conflict_count += inline_evict_recent.conflicts
        evict_recent = EvictResult(
            evict_recent.evicted_units + inline_evict_recent.evicted_units,
            evict_recent.evicted_bytes + inline_evict_recent.evicted_bytes,
            evict_recent.conflicts,
        )
        unit_stats = collect_all_unit_stats(config)

    if do_promote:
        promote_order = (
            sorted(desired_ondeck, key=str)
            + sorted(desired_frequent - desired_ondeck, key=str)
            + sorted(desired_watchlist - desired_frequent - desired_ondeck, key=str)
        )
        for relative_dir in promote_order:
            stats = unit_stats.get(relative_dir)
            if stats is None or stats.size_on_tank < 1 or stats.size_on_cache > 0:
                continue
            result = promote_unit(relative_dir, config) if not config.dry_run else MoveResult(moved_bytes=stats.size_on_tank)
            conflict_count += result.conflicts
            if result.moved_bytes > 0:
                promoted_units += 1
                promote_result = MoveResult(promote_result.moved_bytes + result.moved_bytes, promote_result.conflicts)
        if not config.dry_run:
            unit_stats = collect_all_unit_stats(config)

    _total_bytes, used_bytes, available_bytes = filesystem_usage(config.source_root)

    should_evict = do_evict and (args.demote_non_frequent or available_bytes < config.cache_min_free_space_bytes)

    if should_evict:
        recent_candidates = sorted(
            (unit for unit, stats in unit_stats.items() if stats.size_on_cache > 0 and unit not in protected_units),
            key=lambda unit: unit_age_key(unit, unit_stats.get(unit), state),
        )
        recent_result, available_bytes, used_delta, recent_conflicts = evict_units(
            recent_candidates,
            unit_stats,
            config,
            available_bytes,
            stop_at_target=not args.demote_non_frequent,
        )
        evict_recent = EvictResult(
            evict_recent.evicted_units + recent_result.evicted_units,
            evict_recent.evicted_bytes + recent_result.evicted_bytes,
            evict_recent.conflicts,
        )
        used_bytes -= used_delta
        conflict_count += recent_conflicts

        if available_bytes < config.cache_target_free_space_bytes:
            watchlist_candidates = sorted(
                (
                    unit
                    for unit in desired_watchlist - desired_frequent - desired_ondeck
                    if unit_stats.get(unit) is not None and unit_stats[unit].size_on_cache > 0
                ),
                key=lambda unit: (watchlist_scores.get(unit, 0), *unit_age_key(unit, unit_stats.get(unit), state)),
            )
            watchlist_result, available_bytes, used_delta, watchlist_conflicts = evict_units(
                watchlist_candidates,
                unit_stats,
                config,
                available_bytes,
                stop_at_target=True,
            )
            evict_watchlist = EvictResult(
                evict_watchlist.evicted_units + watchlist_result.evicted_units,
                evict_watchlist.evicted_bytes + watchlist_result.evicted_bytes,
                evict_watchlist.conflicts,
            )
            used_bytes -= used_delta
            conflict_count += watchlist_conflicts

        if available_bytes < config.cache_target_free_space_bytes:
            frequent_candidates = sorted(
                (
                    unit
                    for unit in desired_frequent - desired_ondeck
                    if unit_stats.get(unit) is not None and unit_stats[unit].size_on_cache > 0
                ),
                key=lambda unit: (hot_scores.get(unit, 0), *unit_age_key(unit, unit_stats.get(unit), state)),
            )
            frequent_result, available_bytes, used_delta, frequent_conflicts = evict_units(
                frequent_candidates,
                unit_stats,
                config,
                available_bytes,
                stop_at_target=True,
            )
            evict_frequent = EvictResult(
                evict_frequent.evicted_units + frequent_result.evicted_units,
                evict_frequent.evicted_bytes + frequent_result.evicted_bytes,
                evict_frequent.conflicts,
            )
            used_bytes -= used_delta
            conflict_count += frequent_conflicts

        if available_bytes < config.cache_target_free_space_bytes:
            ondeck_candidates = sorted(
                (
                    unit
                    for unit in desired_ondeck
                    if unit_stats.get(unit) is not None and unit_stats[unit].size_on_cache > 0
                ),
                key=lambda unit: (ondeck_scores.get(unit, 0), *unit_age_key(unit, unit_stats.get(unit), state)),
            )
            ondeck_result, available_bytes, used_delta, ondeck_conflicts = evict_units(
                ondeck_candidates,
                unit_stats,
                config,
                available_bytes,
                stop_at_target=True,
            )
            evict_ondeck = EvictResult(
                evict_ondeck.evicted_units + ondeck_result.evicted_units,
                evict_ondeck.evicted_bytes + ondeck_result.evicted_bytes,
                evict_ondeck.conflicts,
            )
            used_bytes -= used_delta
            conflict_count += ondeck_conflicts

    if not config.dry_run:
        unit_stats = collect_all_unit_stats(config)
        cache_units = {unit: stats.size_on_cache for unit, stats in unit_stats.items() if stats.size_on_cache > 0}
        update_state(state, cache_units, protected_units)
        write_state(config.state_file, state)
        prune_empty_dirs(config.source_root)
        _total_bytes, used_bytes, available_bytes = filesystem_usage(config.source_root)

    print(
        f"{'dry-run: ' if config.dry_run else 'complete: '}"
        f"synced_units={sync_result.synced_units} synced_bytes={format_bytes(sync_result.synced_bytes)} "
        f"replaced_archive_units={sync_result.replaced_units} "
        f"skipped_unstable_units={sync_result.skipped_units} "
        f"desired_frequent_units={len(desired_frequent)} desired_ondeck_units={len(desired_ondeck)} "
        f"desired_watchlist_units={len(desired_watchlist)} "
        f"promoted_units={promoted_units} "
        f"promoted_bytes={format_bytes(promote_result.moved_bytes)} "
        f"evicted_non_frequent_units={evict_recent.evicted_units} "
        f"evicted_non_frequent_bytes={format_bytes(evict_recent.evicted_bytes)} "
        f"evicted_watchlist_units={evict_watchlist.evicted_units} "
        f"evicted_watchlist_bytes={format_bytes(evict_watchlist.evicted_bytes)} "
        f"evicted_frequent_units={evict_frequent.evicted_units} "
        f"evicted_frequent_bytes={format_bytes(evict_frequent.evicted_bytes)} "
        f"evicted_ondeck_units={evict_ondeck.evicted_units} "
        f"evicted_ondeck_bytes={format_bytes(evict_ondeck.evicted_bytes)} "
        f"conflicts={conflict_count} "
        f"cache_used={format_bytes(used_bytes)} cache_avail={format_bytes(available_bytes)}"
    )
    return 0


def main() -> int:
    args = parse_args(sys.argv[1:])
    config = load_config(args)
    if args.report_cache_effectiveness:
        return report_cache_effectiveness(config, args)
    operations_lock_handle = try_acquire_lock(OPERATIONS_LOCK, wait=args.wait_for_lock)
    if operations_lock_handle is None:
        print("skipped: shared media operations lock is held")
        return 0
    lock_handle = try_acquire_lock(config.state_file.parent / "run.lock", wait=args.wait_for_lock)
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
