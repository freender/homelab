from __future__ import annotations

from pathlib import Path

from homelab.modules import pbs_client_backup, pve_exporters


def write_secret_catalog(
    root: Path,
    name: str,
    template_content: str,
    example_content: str,
) -> Path:
    templates_dir = root / "secrets" / "templates"
    templates_dir.mkdir(parents=True)
    template = templates_dir / f"{name}.env.tpl"
    example = templates_dir / f"{name}.env.tpl.example"
    template.write_text(template_content, encoding="utf-8")
    example.write_text(example_content, encoding="utf-8")
    (root / "secrets" / "catalog.yml").write_text(
        "secrets:\n"
        f"  {name}:\n"
        f"    template: secrets/templates/{name}.env.tpl\n",
        encoding="utf-8",
    )
    return example


def test_pbs_client_backup_uses_example_secret_in_offline_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    example = write_secret_catalog(
        tmp_path,
        "pbs-backup-main",
        "PBS_PASSWORD={{ op://Homelab/PBS Backup Main/password }}\n",
        "PBS_PASSWORD=example\n",
    )

    monkeypatch.setenv("HOMELAB_OFFLINE", "true")

    assert pbs_client_backup.secret_path(tmp_path, "backup-main") == example


def test_pve_exporters_uses_module_template(tmp_path: Path) -> None:
    expected = tmp_path / "pve-exporters" / "templates" / "apcupsd-exporter.env.tpl"

    assert pve_exporters.apcupsd_exporter_env_template(tmp_path) == expected


def test_pve_exporters_ignores_legacy_secret_override(tmp_path: Path) -> None:
    legacy = tmp_path / "secrets" / "apcupsd-exporter.env"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("override\n", encoding="utf-8")
    expected = tmp_path / "pve-exporters" / "templates" / "apcupsd-exporter.env.tpl"

    assert pve_exporters.apcupsd_exporter_env_template(tmp_path) == expected
