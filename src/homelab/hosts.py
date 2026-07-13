from __future__ import annotations

from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CONFIG_KEYS = {"type", "hostname", "user", "sshkey"}
OPTIONAL_CONFIG_KEYS = {"agent", "homelab_state_dir", "ssh_config", "standalone"}
ALLOWED_CONFIG_KEYS = REQUIRED_CONFIG_KEYS | OPTIONAL_CONFIG_KEYS
ALLOWED_HOST_KEYS = {"config", "features"}
ALLOWED_SSH_CONFIG_KEYS = {"hostname", "proxy_jump", "user", "sshkey"}


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
            if _has_enabled_feature(config, feature):
                matching.append(host)

        return matching

    def has(self, host: str, feature: str) -> bool:
        config = self._get_host(host)
        return _has_enabled_feature(config, feature)

    def declared_features(self) -> set[str]:
        """Every feature name that appears under any host's `features:` block.

        Includes features gated off with `deploy: false` / `enabled: false`: a typo
        in a disabled block is still a typo, and we want validate to catch it.
        """
        declared: set[str] = set()
        for config in self.load().values():
            if not isinstance(config, dict):
                continue
            features = config.get("features")
            if isinstance(features, dict):
                declared.update(features.keys())
        return declared

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


def _has_enabled_feature(config: dict[str, Any], feature: str) -> bool:
    if feature in config:
        return _feature_value_enabled(config[feature], feature)

    features = config.get("features") or {}
    return (
        isinstance(features, dict)
        and feature in features
        and _feature_value_enabled(features[feature], feature)
    )


def _feature_value_enabled(value: object, feature: str | None = None) -> bool:
    if value is False:
        return False
    if isinstance(value, dict):
        # `deploy: false` is the host-level targeting gate (skip deployment;
        # never touch the running service). The legacy `enabled: false` spelling
        # is still honored for a transition but is deprecated in favor of
        # `deploy`. `deploy` wins when both are present.
        if "deploy" in value:
            return value.get("deploy") is not False
        if value.get("enabled") is False:
            _warn_legacy_enabled(feature)
            return False
    return True


def _warn_legacy_enabled(feature: str | None) -> None:
    import warnings

    target = f" for feature '{feature}'" if feature else ""
    warnings.warn(
        f"host-level 'enabled: false'{target} is deprecated; "
        "use 'deploy: false' instead",
        DeprecationWarning,
        stacklevel=3,
    )


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

    if "standalone" in host_config and not isinstance(host_config["standalone"], bool):
        raise ValueError(f"config.standalone must be a boolean for {host}")

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
