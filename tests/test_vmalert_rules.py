from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from homelab.modules import vmalert_rules
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]


def test_validate_requires_the_complete_active_rule_set(tmp_path: Path) -> None:
    configs_dir = tmp_path / "vmalert-rules" / "configs"
    configs_dir.mkdir(parents=True)
    scripts_dir = tmp_path / "vmalert-rules" / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    for rule_file in vmalert_rules.RULE_FILES:
        (configs_dir / rule_file).write_text("groups: []\n", encoding="utf-8")
    logs_dir = configs_dir / "logs"
    logs_dir.mkdir()
    for rule_file in vmalert_rules.LOG_RULE_FILES:
        (logs_dir / rule_file).write_text("groups: []\n", encoding="utf-8")

    vmalert_rules.validate(tmp_path, [])

    (configs_dir / "unexpected.yml").write_text("groups: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="configs must be exactly"):
        vmalert_rules.validate(tmp_path, [])


def test_validate_requires_the_complete_log_rule_set(tmp_path: Path) -> None:
    """The log rules are their own exact set, so a stray file in configs/logs/
    cannot reach vmalert-logs unreviewed, and a missing one cannot vanish."""
    configs_dir = tmp_path / "vmalert-rules" / "configs"
    (configs_dir / "logs").mkdir(parents=True)
    scripts_dir = tmp_path / "vmalert-rules" / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    for rule_file in vmalert_rules.RULE_FILES:
        (configs_dir / rule_file).write_text("groups: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="configs/logs must be exactly"):
        vmalert_rules.validate(tmp_path, [])


def test_validate_refuses_hosts_other_than_helm(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="supports helm only: neo"):
        vmalert_rules.validate(ROOT, ["neo"])


def test_the_repo_log_rules_are_vlogs_groups() -> None:
    """vmalert-logs defaults to vlogs, but an explicit type keeps a file readable
    on its own and fails loudly if it is ever pointed at the metrics vmalert."""
    import yaml

    for name in vmalert_rules.LOG_RULE_FILES:
        doc = yaml.safe_load((ROOT / "vmalert-rules/configs/logs" / name).read_text())
        for group in doc["groups"]:
            assert group["type"] == "vlogs", f"{name}:{group['name']}"
            for rule in group["rules"]:
                assert "host" in rule["expr"] or "host" in rule.get("labels", {}), rule["alert"]


# ---------------------------------------------------------------------------
# Remote installer (freender/homelab-ops#30, #348)
#
# The rules directories are real tmp_paths and docker is faked. Nothing here
# reaches a real /mnt/cache, a real container, or the network.
# ---------------------------------------------------------------------------

INSTALLER_PATH = ROOT / "vmalert-rules" / "scripts" / "install.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("vmalert_rules_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stage(tmp_path: Path, subdir: str, rules: dict[str, str]) -> Path:
    rules_dir = tmp_path / subdir
    rules_dir.mkdir(parents=True, exist_ok=True)
    for name, content in rules.items():
        (rules_dir / name).write_text(content, encoding="utf-8")
    return rules_dir


def make_ctx(tmp_path: Path, **lists: tuple[str, ...]) -> InstallContext:
    return InstallContext(
        host="helm",
        script_dir=tmp_path,
        build_dir=tmp_path / "build",
        env={},
        deploy_env={key: " ".join(names) for key, names in lists.items()},
        file_map={},
        force_update=False,
    )


class FakeDocker:
    """Records docker invocations and lets a test fail one container's steps."""

    def __init__(
        self,
        dry_run_fail: tuple[str, ...] = (),
        restart_fail: tuple[str, ...] = (),
        missing: tuple[str, ...] = (),
        image: str = "victoriametrics/vmalert:v9.9.9",
    ) -> None:
        self.dry_run_fail = dry_run_fail
        self.restart_fail = restart_fail
        self.missing = missing
        self.image = image
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[:2] == ["docker", "inspect"]:
            code = 1 if command[-1] in self.missing else 0
            return subprocess.CompletedProcess(command, code, stdout=f"{self.image}\n", stderr="")
        if command[:2] == ["docker", "run"]:
            mount = command[command.index("-v") + 1]
            failed = any(mount.startswith(f"{d}:") for d in self.dry_run_fail)
            return subprocess.CompletedProcess(
                command, 1 if failed else 0, stdout="", stderr="bad rule at line 3"
            )
        if command[:2] == ["docker", "restart"]:
            code = 1 if command[2] in self.restart_fail else 0
            return subprocess.CompletedProcess(command, code, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    def restarted(self, container: str) -> bool:
        return ["docker", "restart", container] in self.calls


def _docker(monkeypatch: pytest.MonkeyPatch, installer, fake: FakeDocker) -> FakeDocker:
    monkeypatch.setattr(installer, "_run", fake)
    return fake


def rule_set(installer, tmp_path: Path, live: dict[str, str] | None = None, **kw):
    """The metrics rule set, pointed at a tmp destination."""
    dest = tmp_path / kw.pop("dest", "live")
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in (live or {}).items():
        (dest / name).write_text(content, encoding="utf-8")
    base = dict(
        env_key="VMALERT_RULES", staged="rules", dest_dir=dest, container="vmalert"
    )
    base.update(kw)
    return installer.RuleSet(**base)


def log_rule_set(installer, tmp_path: Path, live: dict[str, str] | None = None):
    return rule_set(
        installer,
        tmp_path,
        live,
        dest="live-logs",
        env_key="VMALERT_LOG_RULES",
        staged="rules/logs",
        container="vmalert-logs",
        validate_flags=("-rule.defaultRuleType=vlogs",),
    )


def test_installer_expects_the_orchestrators_rule_list(tmp_path: Path) -> None:
    """The single-source-of-truth fix: the bash kept its own `require_file` list,
    which had drifted to six entries while the module grew to sixteen."""
    installer = load_installer()

    with pytest.raises(InstallError, match="VMALERT_RULES is empty"):
        installer.expected_rules(make_ctx(tmp_path), "VMALERT_RULES")


def test_installer_refuses_a_partial_upload(tmp_path: Path) -> None:
    """A missing rule means an alert silently stops being enforced."""
    installer = load_installer()
    rules_dir = stage(tmp_path, "rules", {"ups.yml": "groups: []\n"})

    with pytest.raises(InstallError, match="missing: zfs-pools.yml"):
        installer.verify_staged(rules_dir, ("ups.yml", "zfs-pools.yml"), "VMALERT_RULES")


def test_installer_refuses_an_unexpected_staged_rule(tmp_path: Path) -> None:
    installer = load_installer()
    rules_dir = stage(tmp_path, "rules", {"ups.yml": "a\n", "stale.yml": "b\n"})

    with pytest.raises(InstallError, match="do not match VMALERT_RULES .*unexpected: stale.yml"):
        installer.verify_staged(rules_dir, ("ups.yml",), "VMALERT_RULES")


def test_installer_refuses_a_missing_staged_directory(tmp_path: Path) -> None:
    installer = load_installer()

    with pytest.raises(InstallError, match="missing staged rules directory"):
        installer.verify_staged(tmp_path / "absent", ("ups.yml",), "VMALERT_RULES")


def test_staged_log_rules_do_not_count_as_metric_rules(tmp_path: Path) -> None:
    """configs/ is staged recursively, so the log rules sit in rules/logs/. The
    metrics set must look at rules/*.yml only, or every deploy would refuse."""
    installer = load_installer()
    rules_dir = stage(tmp_path, "rules", {"ups.yml": "a\n"})
    stage(tmp_path, "rules/logs", {"kernel.yml": "b\n"})

    installer.verify_staged(rules_dir, ("ups.yml",), "VMALERT_RULES")


def test_installer_refuses_an_unmanaged_rule_at_the_destination(tmp_path: Path) -> None:
    """Refused rather than deleted: an unmanaged rule exists in no repo, so
    removing it would destroy the only copy."""
    installer = load_installer()
    rs = rule_set(installer, tmp_path, {"ups.yml": "a\n", "handwritten.yml": "b\n"})

    with pytest.raises(InstallError, match="unmanaged active vmalert rule"):
        installer.verify_destination_is_managed(rs.dest_dir, ("ups.yml",))


def test_installer_refuses_when_the_destination_directory_is_missing(tmp_path: Path) -> None:
    """The appdata mount not being there is a much bigger problem than this
    module, and creating the directory would paper over it."""
    installer = load_installer()

    with pytest.raises(InstallError, match="rules directory is missing"):
        installer.verify_destination_is_managed(tmp_path / "absent", ("ups.yml",))


def test_rules_are_validated_before_being_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is the contract. Installing first and validating after would
    leave the bad rule on disk, with vmalert serving the last good config and no
    obvious signal that alerting had frozen."""
    installer = load_installer()
    rs = rule_set(installer, tmp_path)
    fake = _docker(monkeypatch, installer, FakeDocker(dry_run_fail=(str(tmp_path / "rules"),)))
    stage(tmp_path, "rules", {"ups.yml": "groups: []\n"})

    with pytest.raises(InstallError, match="vmalert rejected the staged rules"):
        installer.install(make_ctx(tmp_path, VMALERT_RULES=("ups.yml",)), (rs,))

    assert list(rs.dest_dir.glob("*.yml")) == []
    assert not fake.restarted("vmalert")


def test_a_bad_log_rule_blocks_the_metric_rules_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sets are validated before either is installed, so one bad LogsQL
    rule cannot leave the deploy half-applied."""
    installer = load_installer()
    metrics = rule_set(installer, tmp_path, {"ups.yml": "old\n"})
    logs = log_rule_set(installer, tmp_path)
    stage(tmp_path, "rules", {"ups.yml": "new\n"})
    stage(tmp_path, "rules/logs", {"kernel.yml": "bad\n"})
    fake = _docker(
        monkeypatch, installer, FakeDocker(dry_run_fail=(str(tmp_path / "rules/logs"),))
    )
    ctx = make_ctx(tmp_path, VMALERT_RULES=("ups.yml",), VMALERT_LOG_RULES=("kernel.yml",))

    with pytest.raises(InstallError, match="vmalert-logs rejected the staged rules"):
        installer.install(ctx, (metrics, logs))

    assert (metrics.dest_dir / "ups.yml").read_text(encoding="utf-8") == "old\n"
    assert not fake.restarted("vmalert")


def test_validation_surfaces_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Without the stderr passthrough the operator gets 'validation failed' and
    no way to find the offending file."""
    installer = load_installer()
    rs = rule_set(installer, tmp_path)
    _docker(monkeypatch, installer, FakeDocker(dry_run_fail=(str(tmp_path / "rules"),)))

    with pytest.raises(InstallError):
        installer.validate_rules(rs, tmp_path / "rules")

    assert "bad rule at line 3" in capsys.readouterr().out


def test_validation_uses_the_image_the_container_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validating against a different version could accept syntax the running
    vmalert rejects. A hard-coded tag had drifted 38 minor versions behind."""
    installer = load_installer()
    rs = rule_set(installer, tmp_path)
    fake = _docker(monkeypatch, installer, FakeDocker(image="victoriametrics/vmalert:v1.155.0"))

    installer.validate_rules(rs, tmp_path / "rules")

    assert fake.calls[0] == ["docker", "inspect", "--format", "{{.Config.Image}}", "vmalert"]
    run = fake.calls[1]
    assert run[:2] == ["docker", "run"]
    assert "victoriametrics/vmalert:v1.155.0" in run
    assert run[-1] == "-dryRun"
    assert "-rule.defaultRuleType=vlogs" not in run


def test_log_rules_are_validated_as_vlogs_against_vmalert_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the vlogs default type, vmalert parses the LogsQL as MetricsQL and
    rejects every rule, so the flag has to reach the dry run."""
    installer = load_installer()
    rs = log_rule_set(installer, tmp_path)
    fake = _docker(monkeypatch, installer, FakeDocker())

    installer.validate_rules(rs, tmp_path / "rules/logs")

    assert fake.calls[0][-1] == "vmalert-logs"
    run = fake.calls[1]
    assert f"{tmp_path / 'rules/logs'}:/rules:ro" in run
    assert run[-2:] == ["-rule.defaultRuleType=vlogs", "-dryRun"]


def test_the_shipped_rule_sets_match_the_orchestrator() -> None:
    installer = load_installer()
    by_key = {rs.env_key: rs for rs in installer.RULE_SETS}

    assert str(by_key["VMALERT_RULES"].dest_dir) == vmalert_rules.REMOTE_RULES_DIR
    assert str(by_key["VMALERT_LOG_RULES"].dest_dir) == vmalert_rules.REMOTE_LOG_RULES_DIR
    assert by_key["VMALERT_LOG_RULES"].staged == f"rules/{vmalert_rules.LOG_RULES_SUBDIR}"
    assert by_key["VMALERT_LOG_RULES"].container == "vmalert-logs"
    assert by_key["VMALERT_LOG_RULES"].validate_flags == ("-rule.defaultRuleType=vlogs",)
    assert by_key["VMALERT_RULES"].validate_flags == ()


def test_validation_refuses_when_the_container_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No container means no version to validate against; guessing one would
    reintroduce the drift this lookup exists to remove."""
    installer = load_installer()
    rs = rule_set(installer, tmp_path)
    fake = _docker(monkeypatch, installer, FakeDocker(missing=("vmalert",)))

    with pytest.raises(InstallError, match="container is missing: vmalert"):
        installer.validate_rules(rs, tmp_path / "rules")

    assert not any(call[:2] == ["docker", "run"] for call in fake.calls)


def test_validation_refuses_an_empty_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    rs = rule_set(installer, tmp_path)
    _docker(monkeypatch, installer, FakeDocker(image=""))

    with pytest.raises(InstallError, match="could not read the image of vmalert"):
        installer.validate_rules(rs, tmp_path / "rules")


def test_changed_rules_are_installed_and_vmalert_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    rs = rule_set(installer, tmp_path, {"ups.yml": "old\n"})
    fake = _docker(monkeypatch, installer, FakeDocker())
    stage(tmp_path, "rules", {"ups.yml": "new\n"})

    installer.install(make_ctx(tmp_path, VMALERT_RULES=("ups.yml",)), (rs,))

    assert (rs.dest_dir / "ups.yml").read_text(encoding="utf-8") == "new\n"
    assert fake.restarted("vmalert")


def test_unchanged_rules_do_not_restart_vmalert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart drops the in-memory state of every active alert, so an
    unconditional one would re-fire pending alerts on every deploy."""
    installer = load_installer()
    rs = rule_set(installer, tmp_path, {"ups.yml": "same\n"})
    fake = _docker(monkeypatch, installer, FakeDocker())
    stage(tmp_path, "rules", {"ups.yml": "same\n"})

    installer.install(make_ctx(tmp_path, VMALERT_RULES=("ups.yml",)), (rs,))

    assert not fake.restarted("vmalert")


def test_only_the_container_whose_rules_changed_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log-rule edit must not drop the metric alerts' in-memory state."""
    installer = load_installer()
    metrics = rule_set(installer, tmp_path, {"ups.yml": "same\n"})
    logs = log_rule_set(installer, tmp_path, {"kernel.yml": "old\n"})
    stage(tmp_path, "rules", {"ups.yml": "same\n"})
    stage(tmp_path, "rules/logs", {"kernel.yml": "new\n"})
    fake = _docker(monkeypatch, installer, FakeDocker())
    ctx = make_ctx(tmp_path, VMALERT_RULES=("ups.yml",), VMALERT_LOG_RULES=("kernel.yml",))

    installer.install(ctx, (metrics, logs))

    assert (logs.dest_dir / "kernel.yml").read_text(encoding="utf-8") == "new\n"
    assert fake.restarted("vmalert-logs")
    assert not fake.restarted("vmalert")


def test_one_changed_rule_among_many_still_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop must not stop tracking change after the first unchanged file."""
    installer = load_installer()
    rs = rule_set(installer, tmp_path, {"a.yml": "same\n", "b.yml": "old\n"})
    fake = _docker(monkeypatch, installer, FakeDocker())
    stage(tmp_path, "rules", {"a.yml": "same\n", "b.yml": "new\n"})

    installer.install(make_ctx(tmp_path, VMALERT_RULES=("a.yml", "b.yml")), (rs,))

    assert fake.restarted("vmalert")


def test_a_failed_restart_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New rules on disk with a dead vmalert is worse than either alone, and the
    bash discarded `docker restart`'s status."""
    installer = load_installer()
    rs = rule_set(installer, tmp_path, {"ups.yml": "old\n"})
    _docker(monkeypatch, installer, FakeDocker(restart_fail=("vmalert",)))
    stage(tmp_path, "rules", {"ups.yml": "new\n"})

    with pytest.raises(InstallError, match="failed to restart vmalert"):
        installer.install(make_ctx(tmp_path, VMALERT_RULES=("ups.yml",)), (rs,))


def test_rules_are_installed_with_the_mode_the_live_directory_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    rs = rule_set(installer, tmp_path)
    _docker(monkeypatch, installer, FakeDocker())
    stage(tmp_path, "rules", {"ups.yml": "new\n"})

    installer.install(make_ctx(tmp_path, VMALERT_RULES=("ups.yml",)), (rs,))

    assert (rs.dest_dir / "ups.yml").stat().st_mode & 0o777 == 0o644


def test_orchestrator_sends_both_rule_sets_to_the_installer() -> None:
    """The installer refuses anything that is not exactly these lists, so an
    edit that never reached the env would fail every deploy rather than
    silently install a subset."""
    assert len(vmalert_rules.RULE_FILES) == 25
    assert vmalert_rules.LOG_RULE_FILES == ("kernel.yml", "zion.yml")
