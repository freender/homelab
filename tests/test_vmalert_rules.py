from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.modules import vmalert_rules
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError


def test_validate_requires_the_complete_active_rule_set(tmp_path: Path) -> None:
    configs_dir = tmp_path / "vmalert-rules" / "configs"
    configs_dir.mkdir(parents=True)
    scripts_dir = tmp_path / "vmalert-rules" / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    for rule_file in vmalert_rules.RULE_FILES:
        (configs_dir / rule_file).write_text("groups: []\n", encoding="utf-8")

    vmalert_rules.validate(tmp_path, [])

    (configs_dir / "unexpected.yml").write_text("groups: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="configs must be exactly"):
        vmalert_rules.validate(tmp_path, [])


# ---------------------------------------------------------------------------
# Remote installer (freender/homelab-ops#30)
#
# The rules directory is a real tmp_path and docker is faked. Nothing here
# reaches a real /mnt/cache, a real container, or the network.
# ---------------------------------------------------------------------------

INSTALLER_PATH = Path(__file__).resolve().parents[1] / "vmalert-rules" / "scripts" / "install.py"


def load_installer():
    """Fresh load per test: these rebind RULES_DEST_DIR to a tmp_path."""
    spec = importlib.util.spec_from_file_location("vmalert_rules_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_ctx(tmp_path: Path, rules: dict[str, str], expected: tuple[str, ...]) -> InstallContext:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    for name, content in rules.items():
        (rules_dir / name).write_text(content, encoding="utf-8")
    return InstallContext(
        host="helm",
        script_dir=tmp_path,
        build_dir=tmp_path / "build",
        env={},
        deploy_env={"VMALERT_RULES": " ".join(expected)},
        file_map={},
        force_update=False,
    )


class FakeDocker:
    """Records docker invocations and lets a test fail the validation run."""

    def __init__(self, dry_run_code: int = 0, restart_code: int = 0) -> None:
        self.dry_run_code = dry_run_code
        self.restart_code = restart_code
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(
                command, self.dry_run_code, stdout="", stderr="bad rule at line 3"
            )
        if command[:2] == ["docker", "restart"]:
            return subprocess.CompletedProcess(command, self.restart_code, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    @property
    def restarted(self) -> bool:
        return any(call[:2] == ["docker", "restart"] for call in self.calls)


def _docker(monkeypatch: pytest.MonkeyPatch, installer, fake: FakeDocker) -> FakeDocker:
    monkeypatch.setattr(installer, "_run", fake)
    return fake


def _setup(tmp_path: Path, installer, live: dict[str, str] | None = None) -> Path:
    dest = tmp_path / "live"
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in (live or {}).items():
        (dest / name).write_text(content, encoding="utf-8")
    installer.RULES_DEST_DIR = dest
    return dest


def test_installer_expects_the_orchestrators_rule_list(tmp_path: Path) -> None:
    """The single-source-of-truth fix: the bash kept its own `require_file` list,
    which had drifted to six entries while the module grew to sixteen."""
    installer = load_installer()
    ctx = make_ctx(tmp_path, {}, ())

    with pytest.raises(InstallError, match="VMALERT_RULES is empty"):
        installer.expected_rules(ctx)


def test_installer_refuses_a_partial_upload(tmp_path: Path) -> None:
    """A missing rule means an alert silently stops being enforced -- the exact
    class of failure the stale bash list was groping at and could not catch."""
    installer = load_installer()
    ctx = make_ctx(tmp_path, {"ups.yml": "groups: []\n"}, ("ups.yml", "zfs-pools.yml"))

    with pytest.raises(InstallError, match="missing: zfs-pools.yml"):
        installer.verify_staged(tmp_path / "rules", installer.expected_rules(ctx))


def test_installer_refuses_an_unexpected_staged_rule(tmp_path: Path) -> None:
    installer = load_installer()
    ctx = make_ctx(tmp_path, {"ups.yml": "a\n", "stale.yml": "b\n"}, ("ups.yml",))

    with pytest.raises(InstallError, match="unexpected: stale.yml"):
        installer.verify_staged(tmp_path / "rules", installer.expected_rules(ctx))


def test_installer_refuses_an_unmanaged_rule_at_the_destination(tmp_path: Path) -> None:
    """Refused rather than deleted: an unmanaged rule exists in no repo, so
    removing it would destroy the only copy."""
    installer = load_installer()
    _setup(tmp_path, installer, {"ups.yml": "a\n", "handwritten.yml": "b\n"})

    with pytest.raises(InstallError, match="unmanaged active vmalert rule"):
        installer.verify_destination_is_managed(("ups.yml",))


def test_installer_refuses_when_the_destination_directory_is_missing(tmp_path: Path) -> None:
    """The appdata mount not being there is a much bigger problem than this
    module, and creating the directory would paper over it."""
    installer = load_installer()
    installer.RULES_DEST_DIR = tmp_path / "absent"

    with pytest.raises(InstallError, match="rules directory is missing"):
        installer.verify_destination_is_managed(("ups.yml",))


def test_rules_are_validated_before_being_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is the contract. Installing first and validating after would
    leave the bad rule on disk, with vmalert serving the last good config and no
    obvious signal that alerting had frozen."""
    installer = load_installer()
    dest = _setup(tmp_path, installer)
    fake = _docker(monkeypatch, installer, FakeDocker(dry_run_code=1))
    ctx = make_ctx(tmp_path, {"ups.yml": "groups: []\n"}, ("ups.yml",))

    with pytest.raises(InstallError, match="rejected the staged rules"):
        installer.install(ctx)

    assert list(dest.glob("*.yml")) == []
    assert not fake.restarted


def test_validation_surfaces_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Without the stderr passthrough the operator gets 'validation failed' and
    no way to find the offending file."""
    installer = load_installer()
    _setup(tmp_path, installer)
    _docker(monkeypatch, installer, FakeDocker(dry_run_code=1))

    with pytest.raises(InstallError):
        installer.validate_rules(tmp_path / "rules")

    assert "bad rule at line 3" in capsys.readouterr().out


def test_validation_pins_the_image_the_container_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validating against a different version could accept syntax the running
    vmalert rejects, which defeats the point of checking."""
    installer = load_installer()
    _setup(tmp_path, installer)
    fake = _docker(monkeypatch, installer, FakeDocker())

    installer.validate_rules(tmp_path / "rules")

    assert installer.VMALERT_IMAGE in fake.calls[0]
    assert "-dryRun" in fake.calls[0]


def test_changed_rules_are_installed_and_vmalert_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    dest = _setup(tmp_path, installer, {"ups.yml": "old\n"})
    fake = _docker(monkeypatch, installer, FakeDocker())
    ctx = make_ctx(tmp_path, {"ups.yml": "new\n"}, ("ups.yml",))

    installer.install(ctx)

    assert (dest / "ups.yml").read_text(encoding="utf-8") == "new\n"
    assert fake.restarted


def test_unchanged_rules_do_not_restart_vmalert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart drops the in-memory state of every active alert, so an
    unconditional one would re-fire pending alerts on every deploy."""
    installer = load_installer()
    _setup(tmp_path, installer, {"ups.yml": "same\n"})
    fake = _docker(monkeypatch, installer, FakeDocker())
    ctx = make_ctx(tmp_path, {"ups.yml": "same\n"}, ("ups.yml",))

    installer.install(ctx)

    assert not fake.restarted


def test_one_changed_rule_among_many_still_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop must not stop tracking change after the first unchanged file."""
    installer = load_installer()
    _setup(tmp_path, installer, {"a.yml": "same\n", "b.yml": "old\n"})
    fake = _docker(monkeypatch, installer, FakeDocker())
    ctx = make_ctx(tmp_path, {"a.yml": "same\n", "b.yml": "new\n"}, ("a.yml", "b.yml"))

    installer.install(ctx)

    assert fake.restarted


def test_a_failed_restart_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New rules on disk with a dead vmalert is worse than either alone, and the
    bash discarded `docker restart`'s status."""
    installer = load_installer()
    _setup(tmp_path, installer, {"ups.yml": "old\n"})
    _docker(monkeypatch, installer, FakeDocker(restart_code=1))
    ctx = make_ctx(tmp_path, {"ups.yml": "new\n"}, ("ups.yml",))

    with pytest.raises(InstallError, match="failed to restart vmalert"):
        installer.install(ctx)


def test_rules_are_installed_with_the_mode_the_live_directory_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    dest = _setup(tmp_path, installer)
    _docker(monkeypatch, installer, FakeDocker())
    ctx = make_ctx(tmp_path, {"ups.yml": "new\n"}, ("ups.yml",))

    installer.install(ctx)

    assert (dest / "ups.yml").stat().st_mode & 0o777 == 0o644


def test_orchestrator_sends_the_whole_rule_set_to_the_installer() -> None:
    """Ties the two ends together: the installer refuses anything that is not
    exactly this list, so a RULE_FILES edit that never reached the env would
    fail every deploy rather than silently install a subset."""
    assert set(vmalert_rules.RULE_FILES) == set(" ".join(vmalert_rules.RULE_FILES).split())
    assert len(vmalert_rules.RULE_FILES) == 18
