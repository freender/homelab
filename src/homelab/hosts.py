from __future__ import annotations

from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CONFIG_KEYS = {"type", "hostname", "user", "sshkey"}
OPTIONAL_CONFIG_KEYS = {"agent", "ssh_config", "zfs_mountpoint", "zfs_pool"}
ALLOWED_CONFIG_KEYS = REQUIRED_CONFIG_KEYS | OPTIONAL_CONFIG_KEYS
ALLOWED_HOST_KEYS = {"config", "features", "media_storage", "media_storage_ref"}
ALLOWED_SSH_CONFIG_KEYS = {"user", "sshkey"}
ALLOWED_MEDIA_STORAGE_KEYS = {"data_slots", "parity_slots", "raw", "export"}
ALLOWED_MEDIA_STORAGE_RAW_KEYS = {"data_mount_prefix", "parity_mount_prefix"}
ALLOWED_MEDIA_STORAGE_EXPORT_KEYS = {
    "data_mount_prefix",
    "parity_mount_prefix",
    "cache_media_path",
    "merged_media_path",
    "hdd_only_media_path",
}


class _Missing:
    pass


_MISSING = _Missing()


class HostLookupError(KeyError):
    pass


@dataclass(frozen=True)
class HostRegistry:
    path: Path

    @cached_property
    def _data(self) -> dict[str, dict[str, Any]]:
        with self.path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        validate_hosts_data(data, self.path)
        return data

    def load(self) -> dict[str, dict[str, Any]]:
        return self._data

    def list_hosts(self, feature: str | None = None) -> list[str]:
        hosts = self.load()
        if feature is None:
            return list(hosts.keys())

        matching: list[str] = []
        for host, config in hosts.items():
            if not isinstance(config, dict):
                continue
            features = config.get("features") or {}
            if feature in config or (isinstance(features, dict) and feature in features):
                matching.append(host)

        return matching

    def has(self, host: str, feature: str) -> bool:
        config = self._get_host(host)
        features = config.get("features") or {}
        return feature in config or (isinstance(features, dict) and feature in features)

    def get(self, host: str, key: str, default: Any = _MISSING) -> Any:
        config = self._get_host(host)

        top_level = _resolve_dotted_key(config, key)
        if top_level is not _MISSING and top_level is not None:
            return top_level

        features = config.get("features") or {}
        if isinstance(features, dict):
            nested = _resolve_dotted_key(features, key)
            if nested is not _MISSING and nested is not None:
                return nested

        if default is not _MISSING:
            return default

        raise HostLookupError(f"missing key '{key}' for host '{host}'")

    def filter_hosts(self, requested: str | None, supported: list[str]) -> list[str]:
        if requested in (None, "", "all"):
            return list(supported)
        if requested in supported:
            return [requested]
        return []

    def _get_host(self, host: str) -> dict[str, Any]:
        hosts = self.load()
        try:
            config = hosts[host]
        except KeyError as exc:
            raise HostLookupError(f"unknown host '{host}'") from exc

        if not isinstance(config, dict):
            raise ValueError(f"host entry must be a mapping: {host}")

        return config


def _resolve_dotted_key(data: dict[str, Any], key: str) -> Any:
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def validate_hosts_data(data: object, path: Path) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"hosts file must contain a mapping: {path}")

    for host, config in data.items():
        if not isinstance(config, dict):
            raise ValueError(f"host entry must be a mapping: {host}")

        unknown_host_keys = sorted(set(config) - ALLOWED_HOST_KEYS)
        if unknown_host_keys:
            keys = ", ".join(unknown_host_keys)
            raise ValueError(f"unknown top-level key(s) for {host}: {keys}")

        validate_host_config(host, config)
        validate_host_features(host, config.get("features"))
        validate_media_storage(host, config.get("media_storage"))
        validate_media_storage_ref(host, config.get("media_storage_ref"))


def validate_host_config(host: str, config: dict[str, Any]) -> None:
    host_config = config.get("config")
    if not isinstance(host_config, dict):
        raise ValueError(f"config must be a mapping for {host}")

    missing_keys = sorted(REQUIRED_CONFIG_KEYS - set(host_config))
    if missing_keys:
        keys = ", ".join(missing_keys)
        raise ValueError(f"missing required config key(s) for {host}: {keys}")

    unknown_keys = sorted(set(host_config) - ALLOWED_CONFIG_KEYS)
    if unknown_keys:
        keys = ", ".join(unknown_keys)
        raise ValueError(f"unknown config key(s) for {host}: {keys}")

    ssh_config = host_config.get("ssh_config")
    if ssh_config is None:
        return
    if not isinstance(ssh_config, dict):
        raise ValueError(f"config.ssh_config must be a mapping for {host}")

    unknown_ssh_keys = sorted(set(ssh_config) - ALLOWED_SSH_CONFIG_KEYS)
    if unknown_ssh_keys:
        keys = ", ".join(unknown_ssh_keys)
        raise ValueError(f"unknown config.ssh_config key(s) for {host}: {keys}")


def validate_host_features(host: str, features: object) -> None:
    if features is not None and not isinstance(features, dict):
        raise ValueError(f"features must be a mapping for {host}")


def validate_media_storage(host: str, media_storage: object) -> None:
    if media_storage is None:
        return
    if not isinstance(media_storage, dict):
        raise ValueError(f"media_storage must be a mapping for {host}")

    unknown_media_storage_keys = sorted(set(media_storage) - ALLOWED_MEDIA_STORAGE_KEYS)
    if unknown_media_storage_keys:
        keys = ", ".join(unknown_media_storage_keys)
        raise ValueError(f"unknown media_storage key(s) for {host}: {keys}")

    validate_optional_mapping_keys(
        host,
        "media_storage.raw",
        media_storage.get("raw"),
        ALLOWED_MEDIA_STORAGE_RAW_KEYS,
    )
    validate_optional_mapping_keys(
        host,
        "media_storage.export",
        media_storage.get("export"),
        ALLOWED_MEDIA_STORAGE_EXPORT_KEYS,
    )


def validate_media_storage_ref(host: str, media_storage_ref: object) -> None:
    if media_storage_ref is None:
        return
    if not isinstance(media_storage_ref, str) or not media_storage_ref.strip():
        raise ValueError(f"media_storage_ref must be a non-empty string for {host}")


def validate_optional_mapping_keys(
    host: str,
    field_name: str,
    value: object,
    allowed_keys: set[str],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping for {host}")

    unknown_keys = sorted(set(value) - allowed_keys)
    if unknown_keys:
        keys = ", ".join(unknown_keys)
        raise ValueError(f"unknown {field_name} key(s) for {host}: {keys}")


@cache
def default_registry(root: Path) -> HostRegistry:
    return HostRegistry(root / "hosts.conf")
