from __future__ import annotations

from pathlib import Path

import pytest

from homelab.modules import apcupsd, pve_backup, pve_exporters


def test_apcupsd_uses_example_secret_in_offline_mode(monkeypatch, tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    example = secrets_dir / "telegram.env.example"
    example.write_text("TELEGRAM_TOKEN=example\n", encoding="utf-8")

    monkeypatch.setenv("HOMELAB_OFFLINE", "true")

    assert apcupsd.telegram_env_path(tmp_path) == example


def test_apcupsd_requires_real_secret_outside_offline_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)

    with pytest.raises(ValueError, match="telegram.env not found"):
        apcupsd.telegram_env_path(tmp_path)


def test_pve_backup_uses_example_secret_in_offline_mode(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    example = secrets_dir / "pbs-backup-main.env.example"
    example.write_text("PBS_PASSWORD=example\n", encoding="utf-8")

    assert pve_backup.secret_path(tmp_path, "backup-main", allow_example=True) == example


def test_pve_exporters_prefers_tracked_example_template(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True)
    example = secrets_dir / "apcupsd-exporter.env.example"
    example.write_text("APCUPSD_EXPORTER_UPS_HOST=\"{{ UPS_HOST }}\"\n", encoding="utf-8")

    assert pve_exporters.apcupsd_exporter_env_template(tmp_path) == example


def test_pve_exporters_prefers_local_env_override_when_present(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True)
    local_template = secrets_dir / "apcupsd-exporter.env"
    local_template.write_text("override\n", encoding="utf-8")
    (secrets_dir / "apcupsd-exporter.env.example").write_text("example\n", encoding="utf-8")

    assert pve_exporters.apcupsd_exporter_env_template(tmp_path) == local_template
