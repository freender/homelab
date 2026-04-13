#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IGNORED_PREFIX = "."
IGNORED_SUFFIXES = (".part", ".tmp", ".partial", ".!qB")


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


def should_skip(path: Path, cutoff: float, ignore_paths: tuple[Path, ...]) -> bool:
    if path.is_symlink() or not path.is_file():
        return True
    if is_ignored(path, ignore_paths):
        return True
    if any(part.startswith(IGNORED_PREFIX) for part in path.parts):
        return True
    if path.name.endswith(IGNORED_SUFFIXES):
        return True
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return True
    if stat_result.st_mtime >= cutoff:
        return True
    return file_is_open(path)


def prune_empty_dirs(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir()]
    directories.sort(key=lambda path: (len(path.relative_to(root).parts), str(path)), reverse=True)
    for directory in directories:
        try:
            directory.rmdir()
            print(f"removed empty directory: {directory}")
        except OSError:
            continue


def move_file(source: Path, source_root: Path, target_root: Path) -> None:
    relative_path = source.relative_to(source_root)
    target = target_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        source_stat = source.stat()
        target_stat = target.stat()
        if (
            source_stat.st_size == target_stat.st_size
            and int(source_stat.st_mtime) == int(target_stat.st_mtime)
        ):
            source.unlink()
            print(f"removed duplicate source file: {source}")
            return
        raise RuntimeError(f"target already exists with different content: {target}")

    shutil.copy2(source, target)
    stat_result = source.stat()
    os.chown(target, stat_result.st_uid, stat_result.st_gid)
    source.unlink()
    print(f"moved: {source} -> {target}")


def main() -> int:
    source_root = Path(require_env("SOURCE_DIR"))
    target_root = Path(require_env("TARGET_DIR"))
    age_days = int(require_env("AGE_DAYS"))
    ignore_paths = parse_ignore_paths(source_root, os.environ.get("IGNORE_PATHS", ""))
    cutoff = time.time() - (age_days * 86400)

    if not source_root.is_dir():
        raise SystemExit(f"source directory does not exist: {source_root}")
    if not target_root.is_dir():
        raise SystemExit(f"target directory does not exist: {target_root}")

    moved = 0
    skipped = 0
    for path in sorted(source_root.rglob("*")):
        if should_skip(path, cutoff, ignore_paths):
            skipped += 1
            continue
        move_file(path, source_root, target_root)
        moved += 1

    prune_empty_dirs(source_root)
    print(f"complete: moved={moved} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
