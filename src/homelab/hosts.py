from __future__ import annotations

from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import Any

import yaml


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

        if not isinstance(data, dict):
            raise ValueError(f"hosts file must contain a mapping: {self.path}")

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


@cache
def default_registry(root: Path) -> HostRegistry:
    return HostRegistry(root / "hosts.conf")
