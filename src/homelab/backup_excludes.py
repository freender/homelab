from __future__ import annotations

from pathlib import Path

PROFILE_DIR = "backup-excludes"


def normalize_profile_names(value: object, message: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(message)
    profiles = []
    for item in value:
        profile = str(item).strip()
        if profile:
            profiles.append(profile)
    return profiles


def load_profile(root: Path, profile: str) -> list[str]:
    if "/" in profile or ".." in profile:
        raise ValueError(f"invalid backup exclude profile name: {profile!r}")
    path = root / PROFILE_DIR / f"{profile}.txt"
    if not path.is_file():
        raise ValueError(f"backup exclude profile not found: {profile}")
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        entries.append(entry)
    return entries


def load_profiles(root: Path, profiles: list[str]) -> list[str]:
    entries = []
    for profile in profiles:
        entries.extend(load_profile(root, profile))
    return entries


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def join_mount_prefix(prefix: str, entry: str) -> str:
    return f"{prefix.rstrip('/')}/{entry.lstrip('/')}"
