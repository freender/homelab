from __future__ import annotations

from pathlib import Path

import pytest

from homelab.modules import monitoring_config


def write_module_files(root: Path) -> None:
    configs_dir = root / "monitoring-config" / "configs"
    configs_dir.mkdir(parents=True)
    (configs_dir / "scrape.yml").write_text("scrape_configs: []\n", encoding="utf-8")
    (configs_dir / "alertmanager.yml.tpl").write_text(
        "chat_id: __TELEGRAM_CHATID__\nchat_id: __TELEGRAM_CHATID_PLEX__\n",
        encoding="utf-8",
    )
    scripts_dir = root / "monitoring-config" / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")


def test_validate_requires_exact_configs_and_placeholders(tmp_path: Path) -> None:
    write_module_files(tmp_path)
    monitoring_config.validate(tmp_path)

    (tmp_path / "monitoring-config" / "configs" / "unexpected.yml").write_text(
        "{}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="configs must be exactly"):
        monitoring_config.validate(tmp_path)


def test_validate_rejects_missing_chat_id_placeholder(tmp_path: Path) -> None:
    write_module_files(tmp_path)
    template = tmp_path / "monitoring-config" / "configs" / "alertmanager.yml.tpl"
    template.write_text("chat_id: __TELEGRAM_CHATID__\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must contain __TELEGRAM_CHATID_PLEX__"):
        monitoring_config.validate(tmp_path)


def test_validate_allows_a_chat_id_reused_by_several_receivers(tmp_path: Path) -> None:
    """The private chat backs both the default and the Proxmox receiver."""
    write_module_files(tmp_path)
    template = tmp_path / "monitoring-config" / "configs" / "alertmanager.yml.tpl"
    template.write_text(
        "chat_id: __TELEGRAM_CHATID__\n"
        "chat_id: __TELEGRAM_CHATID__\n"
        "chat_id: __TELEGRAM_CHATID_PLEX__\n",
        encoding="utf-8",
    )

    monitoring_config.validate(tmp_path)
