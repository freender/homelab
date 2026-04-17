from __future__ import annotations

from dataclasses import dataclass

from .hosts import HostLookupError


@dataclass(frozen=True)
class MediaStorageConfig:
    data_slots: tuple[int, ...]
    parity_slots: tuple[int, ...]
    raw_data_mount_prefix: str | None
    raw_parity_mount_prefix: str | None
    pool_root_path: str | None
    pool_cache_media_path: str | None
    pool_merged_media_path: str | None
    pool_hdd_only_media_path: str | None
    export_data_mount_prefix: str | None
    export_cache_media_path: str | None
    export_merged_media_path: str | None
    export_hdd_only_media_path: str | None

    def raw_mounts(self) -> tuple[tuple[str, str], ...]:
        if self.raw_data_mount_prefix is None or self.raw_parity_mount_prefix is None:
            return ()
        mounts = [
            (f"data{slot}", f"{self.raw_data_mount_prefix}{slot}")
            for slot in self.data_slots
        ]
        mounts.extend(
            (f"parity{slot}", f"{self.raw_parity_mount_prefix}{slot}")
            for slot in self.parity_slots
        )
        return tuple(mounts)

    def raw_data_disks(self) -> tuple[tuple[str, str], ...]:
        if self.raw_data_mount_prefix is None:
            return ()
        return tuple(
            (f"data{slot}", f"{self.raw_data_mount_prefix}{slot}")
            for slot in self.data_slots
        )

    def raw_parity_disks(self) -> tuple[tuple[str, str], ...]:
        if self.raw_parity_mount_prefix is None:
            return ()
        return tuple(
            (f"parity{slot}", f"{self.raw_parity_mount_prefix}{slot}")
            for slot in self.parity_slots
        )

    def raw_content_files(self) -> tuple[str, ...]:
        content_files = [f"{path}/snapraid.content" for _name, path in self.raw_data_disks()]
        content_files.extend(f"{path}/snapraid.content" for _name, path in self.raw_parity_disks())
        return tuple(content_files)

    def raw_media_branches(self) -> tuple[str, ...]:
        if self.raw_data_mount_prefix is None:
            return ()
        return tuple(f"{self.raw_data_mount_prefix}{slot}/media" for slot in self.data_slots)

    def export_idmapped_mounts(self) -> tuple[tuple[str, str], ...]:
        if self.raw_data_mount_prefix is None or self.export_data_mount_prefix is None:
            return ()
        return tuple(
            (
                f"{self.raw_data_mount_prefix}{slot}",
                f"{self.export_data_mount_prefix}{slot}",
            )
            for slot in self.data_slots
        )

    def export_media_branches(self) -> tuple[str, ...]:
        if self.export_data_mount_prefix is None:
            return ()
        return tuple(f"{self.export_data_mount_prefix}{slot}/media" for slot in self.data_slots)


def load_media_storage(registry, host: str) -> MediaStorageConfig | None:
    try:
        data_slots_raw = registry.get(host, "media_storage.data_slots")
    except HostLookupError:
        return None

    data_slots = normalize_slots(data_slots_raw, f"media_storage.data_slots must be a non-empty list for {host}")
    parity_slots = normalize_slots(
        registry.get(host, "media_storage.parity_slots", [1]),
        f"media_storage.parity_slots must be a non-empty list for {host}",
    )

    return MediaStorageConfig(
        data_slots=data_slots,
        parity_slots=parity_slots,
        raw_data_mount_prefix=optional_absolute_prefix(registry.get(host, "media_storage.raw.data_mount_prefix", ""), f"media_storage.raw.data_mount_prefix must be absolute for {host}"),
        raw_parity_mount_prefix=optional_absolute_prefix(registry.get(host, "media_storage.raw.parity_mount_prefix", ""), f"media_storage.raw.parity_mount_prefix must be absolute for {host}"),
        pool_root_path=optional_absolute_path(registry.get(host, "media_storage.pool.root_path", ""), f"media_storage.pool.root_path must be absolute for {host}"),
        pool_cache_media_path=optional_absolute_path(registry.get(host, "media_storage.pool.cache_media_path", ""), f"media_storage.pool.cache_media_path must be absolute for {host}"),
        pool_merged_media_path=optional_absolute_path(registry.get(host, "media_storage.pool.merged_media_path", ""), f"media_storage.pool.merged_media_path must be absolute for {host}"),
        pool_hdd_only_media_path=optional_absolute_path(registry.get(host, "media_storage.pool.hdd_only_media_path", ""), f"media_storage.pool.hdd_only_media_path must be absolute for {host}"),
        export_data_mount_prefix=optional_absolute_prefix(registry.get(host, "media_storage.export.data_mount_prefix", ""), f"media_storage.export.data_mount_prefix must be absolute for {host}"),
        export_cache_media_path=optional_absolute_path(registry.get(host, "media_storage.export.cache_media_path", ""), f"media_storage.export.cache_media_path must be absolute for {host}"),
        export_merged_media_path=optional_absolute_path(registry.get(host, "media_storage.export.merged_media_path", ""), f"media_storage.export.merged_media_path must be absolute for {host}"),
        export_hdd_only_media_path=optional_absolute_path(registry.get(host, "media_storage.export.hdd_only_media_path", ""), f"media_storage.export.hdd_only_media_path must be absolute for {host}"),
    )


def normalize_slots(value: object, message: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(message)
    slots: list[int] = []
    for item in value:
        try:
            slot = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(message) from exc
        if slot < 1 or slot in slots:
            raise ValueError(message)
        slots.append(slot)
    return tuple(slots)


def optional_absolute_path(value: object, message: str) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    if not text.startswith("/"):
        raise ValueError(message)
    return text


def optional_absolute_prefix(value: object, message: str) -> str | None:
    return optional_absolute_path(value, message)
