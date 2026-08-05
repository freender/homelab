from __future__ import annotations

from pathlib import Path

import pytest

from homelab.modules import vmalert_rules


def test_validate_requires_the_complete_active_rule_set(tmp_path: Path) -> None:
    configs_dir = tmp_path / "vmalert-rules" / "configs"
    configs_dir.mkdir(parents=True)
    scripts_dir = tmp_path / "vmalert-rules" / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    for rule_file in vmalert_rules.RULE_FILES:
        (configs_dir / rule_file).write_text("groups: []\n", encoding="utf-8")

    vmalert_rules.validate(tmp_path, [])

    (configs_dir / "unexpected.yml").write_text("groups: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="configs must be exactly"):
        vmalert_rules.validate(tmp_path, [])
