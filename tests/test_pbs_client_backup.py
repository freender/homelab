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
        "archives": (
            pbs_client_backup.ArchivePlan(
                name="etc", dataset="", path="/etc", excludes=()
            ),
        ),
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
