from __future__ import annotations

import stat
from pathlib import Path

import pytest

from homelab import op_secrets
from homelab.modules import pbs_client_backup


def _base_plan(**overrides: object) -> pbs_client_backup.BackupPlan:
    defaults: dict[str, object] = {
        "enabled": True,
        "paused": False,
        "schedule": "*-*-* 00:20:00",
        "repository": "user@pbs@host:backup",
        "namespace": "freender",
        "secret_profile": "backup-main",
        "backup_id": "host",
        "backup_type": "host",
        "host_type": "ubuntu",
        "encrypt": False,
        "purge_keyfile": False,
        "archives": (
            pbs_client_backup.ArchivePlan(
                name="etc", dataset="", path="/etc", excludes=()
            ),
        ),
        "fallback_destinations": (),
    }
    defaults.update(overrides)
    return pbs_client_backup.BackupPlan(**defaults)  # type: ignore[arg-type]


def test_write_config_emits_encrypt_disabled_by_default(tmp_path: Path) -> None:
    path = tmp_path / "conf"
    pbs_client_backup.write_config(path, _base_plan(encrypt=False))
    text = path.read_text(encoding="utf-8")
    assert 'ENCRYPT="false"' in text
    assert f'KEYFILE="{pbs_client_backup.KEYFILE_REMOTE_PATH}"' in text


def test_write_config_emits_encrypt_enabled(tmp_path: Path) -> None:
    path = tmp_path / "conf"
    pbs_client_backup.write_config(path, _base_plan(encrypt=True))
    text = path.read_text(encoding="utf-8")
    assert 'ENCRYPT="true"' in text
    assert f'KEYFILE="{pbs_client_backup.KEYFILE_REMOTE_PATH}"' in text


def test_write_config_emits_purge_keyfile(tmp_path: Path) -> None:
    path = tmp_path / "conf"
    pbs_client_backup.write_config(path, _base_plan(encrypt=False, purge_keyfile=True))
    text = path.read_text(encoding="utf-8")
    assert 'ENCRYPT="false"' in text
    assert 'PURGE_KEYFILE="true"' in text


def test_write_config_purge_keyfile_off_when_encrypting(tmp_path: Path) -> None:
    path = tmp_path / "conf"
    pbs_client_backup.write_config(path, _base_plan(encrypt=True, purge_keyfile=False))
    text = path.read_text(encoding="utf-8")
    assert 'ENCRYPT="true"' in text
    assert 'PURGE_KEYFILE="false"' in text


def test_purge_keyfile_set_when_encryption_disabled() -> None:
    registry = _FakeRegistry({"pbs-client-backup.encrypt": False})
    assert pbs_client_backup.normalize_backup_plan(Path("/"), registry, "cinci").purge_keyfile


def test_purge_keyfile_cleared_for_encrypted_pve_storage() -> None:
    """A PVE host with an encrypted vzdump storage still needs the keyfile."""
    registry = _FakeRegistry({
        "pbs-client-backup.encrypt": False,
        "pve-backup.pbs_setup.storages": [{"name": "backup-local", "encryption": True}],
    })
    plan = pbs_client_backup.normalize_backup_plan(Path("/"), registry, "ace")
    assert plan.encrypt is False
    assert plan.purge_keyfile is False


class _FakeRegistry:
    """Minimal registry stub: overrides plus the fields a plan needs."""

    def __init__(self, overrides: dict[str, object]) -> None:
        self._overrides = overrides

    def get(self, host: str, key: str, default: object = None) -> object:
        if key in self._overrides:
            return self._overrides[key]
        if key == "pbs-client-backup.repository":
            return "user@pbs@localhost:backup"
        if key == "pbs-client-backup.secret_profile":
            return "backup-main"
        if key == "pbs-client-backup.archives":
            return [{"name": "etc", "path": "/etc"}]
        if key == "config.type":
            return "ubuntu"
        return default


def test_write_config_emits_ordered_fallback_destinations(tmp_path: Path) -> None:
    path = tmp_path / "conf"
    plan = _base_plan(
        fallback_destinations=(
            pbs_client_backup.BackupDestination(
                repository="user@pbs@fallback:backup",
                secret_profile="backup-xur-cottonwood",
            ),
        )
    )
    pbs_client_backup.write_config(path, plan)
    text = path.read_text(encoding="utf-8")
    assert 'DESTINATION_COUNT="2"' in text
    assert 'DESTINATION_0_REPOSITORY="user@pbs@host:backup"' in text
    assert 'DESTINATION_1_REPOSITORY="user@pbs@fallback:backup"' in text


def test_stage_encryption_keyfile_writes_raw_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homelab import module_support

    keyfile_json = (
        '{"kdf":null,"created":"2026-01-01T00:00:00+00:00",'
        '"modified":"2026-01-01T00:00:00+00:00","data":"AAA=",'
        '"fingerprint":"aa:bb"}'
    )
    rendered = tmp_path / "rendered.env"
    rendered.write_text(
        f"PBS_ENCRYPTION_KEY={keyfile_json}\n"
        "PBS_ENCRYPTION_FINGERPRINT=aa:bb\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module_support.op_secrets, "secret_file", lambda root, name: rendered
    )

    dest = tmp_path / "out" / "pbs-encryption.key"
    module_support.stage_encryption_keyfile(tmp_path, dest)

    assert dest.read_text(encoding="utf-8") == keyfile_json + "\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_stage_encryption_keyfile_rejects_non_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homelab import module_support

    rendered = tmp_path / "rendered.env"
    rendered.write_text("PBS_ENCRYPTION_KEY=not-json\n", encoding="utf-8")
    monkeypatch.setattr(
        module_support.op_secrets, "secret_file", lambda root, name: rendered
    )

    with pytest.raises(op_secrets.OpSecretsError):
        module_support.stage_encryption_keyfile(tmp_path, tmp_path / "out.key")
