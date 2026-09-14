from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.modules import monitoring_config
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError


def write_module_files(root: Path) -> None:
    configs_dir = root / "monitoring-config" / "configs"
    configs_dir.mkdir(parents=True)
    (configs_dir / "scrape.yml").write_text("scrape_configs: []\n", encoding="utf-8")
    (configs_dir / "alertmanager.yml.tpl").write_text(
        "chat_id: __TELEGRAM_CHATID__\n"
        "chat_id: __TELEGRAM_CHATID_PLEX__\n"
        "url: __HEALTHCHECK_URL__\n",
        encoding="utf-8",
    )
    scripts_dir = root / "monitoring-config" / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")


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


def test_validate_rejects_a_template_without_the_dead_mans_switch(tmp_path: Path) -> None:
    """Dropping the watchdog receiver would silently remove the only external alarm."""
    write_module_files(tmp_path)
    template = tmp_path / "monitoring-config" / "configs" / "alertmanager.yml.tpl"
    template.write_text(
        "chat_id: __TELEGRAM_CHATID__\nchat_id: __TELEGRAM_CHATID_PLEX__\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must contain __HEALTHCHECK_URL__"):
        monitoring_config.validate(tmp_path)


def test_validate_allows_a_chat_id_reused_by_several_receivers(tmp_path: Path) -> None:
    """The private chat backs both the default and the Proxmox receiver."""
    write_module_files(tmp_path)
    template = tmp_path / "monitoring-config" / "configs" / "alertmanager.yml.tpl"
    template.write_text(
        "chat_id: __TELEGRAM_CHATID__\n"
        "chat_id: __TELEGRAM_CHATID__\n"
        "chat_id: __TELEGRAM_CHATID_PLEX__\n"
        "url: __HEALTHCHECK_URL__\n",
        encoding="utf-8",
    )

    monitoring_config.validate(tmp_path)


# ---------------------------------------------------------------------------
# Remote installer (freender/homelab-ops#30)
#
# Destinations are real tmp_path files and docker is faked. Nothing here reaches
# a real /mnt/cache, a real container, or the network.
# ---------------------------------------------------------------------------

INSTALLER_PATH = (
    Path(__file__).resolve().parents[1] / "monitoring-config" / "scripts" / "install.py"
)

TEMPLATE = (
    "chat_id: __TELEGRAM_CHATID__\n"
    "chat_id: __TELEGRAM_CHATID_PLEX__\n"
    "url: __HEALTHCHECK_URL__\n"
)


def load_installer():
    spec = importlib.util.spec_from_file_location("monitoring_config_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDocker:
    """Records docker invocations and lets a test fail any one of them."""

    def __init__(self, **codes: int) -> None:
        self.codes = codes
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        key = self._key(command)
        stdout = "victoriametrics/vmagent:v1.117.1" if key == "inspect" else ""
        return subprocess.CompletedProcess(
            command, self.codes.get(key, 0), stdout=stdout, stderr=f"{key} said no"
        )

    @staticmethod
    def _key(command: list[str]) -> str:
        verb = command[1]
        if verb != "run":
            return verb
        return "amtool" if "--entrypoint" in command else "run"

    def ran(self, key: str) -> bool:
        return any(self._key(call) == key for call in self.calls)


def make_ctx(tmp_path: Path, env: dict[str, str]) -> InstallContext:
    configs = tmp_path / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    return InstallContext(
        host="helm",
        script_dir=tmp_path,
        build_dir=tmp_path / "build",
        env=env,
        deploy_env={},
        file_map={},
        force_update=False,
    )


def stage(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / "configs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def live(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / "live" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def vmagent_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    values = {
        "VMAGENT_CONTAINER": "vmagent-helm",
        "VMAGENT_DEST": str(tmp_path / "live" / "scrape.yml"),
        "ALERTMANAGER_DEST": str(tmp_path / "live" / "alertmanager.yml.tpl"),
        "ALERTMANAGER_ENABLED": "false",
    }
    values.update(extra)
    return values


def test_installer_refuses_when_the_live_config_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mount check, not a bootstrap check: with /mnt/cache/appdata unmounted the
    write would land in the empty directory underneath and the container would
    keep reading its old config while the deploy reported success."""
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker())
    stage(tmp_path, "scrape.yml", "scrape_configs: []\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    with pytest.raises(InstallError, match="missing live vmagent scrape config"):
        installer.install(ctx)


def test_installer_refuses_an_incomplete_env_file(tmp_path: Path) -> None:
    """The destinations are the orchestrator's to own; an env file missing one is
    a failed render, not something to guess a default for."""
    installer = load_installer()
    ctx = make_ctx(tmp_path, {"VMAGENT_CONTAINER": "vmagent-helm"})

    with pytest.raises(InstallError, match="missing: VMAGENT_DEST"):
        installer.install(ctx)


def test_scrape_config_is_validated_before_it_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is the contract. A scrape config vmagent cannot parse leaves it
    serving the last good one, so installing first would freeze collection with
    no obvious signal."""
    installer = load_installer()
    fake = FakeDocker(run=1)
    monkeypatch.setattr(installer, "_run", fake)
    stage(tmp_path, "scrape.yml", "new\n")
    dest = live(tmp_path, "scrape.yml", "old\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    with pytest.raises(InstallError, match="vmagent rejected the staged scrape config"):
        installer.install(ctx)

    assert dest.read_text(encoding="utf-8") == "old\n"
    assert not fake.ran("exec")


def test_validation_surfaces_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker(run=1))

    with pytest.raises(InstallError):
        installer.validate_scrape_config("image:tag", tmp_path / "scrape.yml")

    assert "run said no" in capsys.readouterr().out


def test_validation_pins_the_image_the_container_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validating against a different version could accept syntax the running
    vmagent rejects, which defeats the point of checking."""
    installer = load_installer()
    fake = FakeDocker()
    monkeypatch.setattr(installer, "_run", fake)
    stage(tmp_path, "scrape.yml", "same\n")
    live(tmp_path, "scrape.yml", "same\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    installer.install(ctx)

    assert fake.calls[0][:4] == ["docker", "inspect", "--format", "{{.Config.Image}}"]
    assert "victoriametrics/vmagent:v1.117.1" in fake.calls[1]


def test_a_missing_container_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker(inspect=1))

    with pytest.raises(InstallError, match="container is missing: vmagent-helm"):
        installer.container_image("vmagent-helm")


def test_a_changed_scrape_config_is_installed_and_vmagent_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    fake = FakeDocker()
    monkeypatch.setattr(installer, "_run", fake)
    stage(tmp_path, "scrape.yml", "new\n")
    dest = live(tmp_path, "scrape.yml", "old\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    installer.install(ctx)

    assert dest.read_text(encoding="utf-8") == "new\n"
    assert fake.ran("exec")


def test_an_unchanged_scrape_config_does_not_reload_vmagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    fake = FakeDocker()
    monkeypatch.setattr(installer, "_run", fake)
    stage(tmp_path, "scrape.yml", "same\n")
    live(tmp_path, "scrape.yml", "same\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    installer.install(ctx)

    assert not fake.ran("exec")


def test_a_failed_reload_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new scrape config on disk that vmagent never picked up is worse than
    either outcome alone: the repo and the running collector disagree."""
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker(exec=1))
    stage(tmp_path, "scrape.yml", "new\n")
    live(tmp_path, "scrape.yml", "old\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    with pytest.raises(InstallError, match="failed to reload vmagent-helm"):
        installer.install(ctx)


def test_alertmanager_is_skipped_where_it_is_not_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """neo scrapes and forwards; only helm runs Alertmanager. The template is not
    even staged there, so touching it would fail rather than no-op."""
    installer = load_installer()
    fake = FakeDocker()
    monkeypatch.setattr(installer, "_run", fake)
    stage(tmp_path, "scrape.yml", "same\n")
    live(tmp_path, "scrape.yml", "same\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path, ALERTMANAGER_ENABLED="false"))

    installer.install(ctx)

    assert not fake.ran("amtool")


def test_a_typo_in_the_alertmanager_flag_fails_rather_than_disabling_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[[ "$X" != "true" ]]` mapped every typo to 'not managed', which would
    silently stop deploying Alertmanager's config while reporting success."""
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker())
    stage(tmp_path, "scrape.yml", "same\n")
    live(tmp_path, "scrape.yml", "same\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path, ALERTMANAGER_ENABLED="ture"))

    with pytest.raises(InstallError, match="ALERTMANAGER_ENABLED must be true or false"):
        installer.install(ctx)


def test_the_compose_file_is_taken_from_the_destination_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One path owner. A separately pinned compose path could drift onto a
    different appdata directory than the template it is supposed to render."""
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker())
    stage(tmp_path, "alertmanager.yml.tpl", TEMPLATE)
    live(tmp_path, "alertmanager.yml.tpl", TEMPLATE)
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    with pytest.raises(InstallError, match="missing Alertmanager compose file"):
        installer.install_alertmanager_template(ctx, ctx.env["ALERTMANAGER_DEST"])


def test_the_validation_render_substitutes_every_placeholder(tmp_path: Path) -> None:
    """The real healthcheck URL is a capability that must not appear in a public
    repo, so the template ships placeholders and amtool gets dummies."""
    installer = load_installer()
    out = tmp_path / "rendered.yml"

    installer.render_for_validation(stage(tmp_path, "t.tpl", TEMPLATE), out)

    rendered = out.read_text(encoding="utf-8")
    assert "__" not in rendered
    assert "example.net" in rendered


def test_a_changed_template_recreates_alertmanager_and_rechecks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check-config above ran against dummy substitutions, so it proves the shape
    and nothing about the secrets the entrypoint injects. The in-container run is
    what checks the file Alertmanager is really using."""
    installer = load_installer()
    fake = FakeDocker()
    monkeypatch.setattr(installer, "_run", fake)
    stage(tmp_path, "alertmanager.yml.tpl", TEMPLATE)
    dest = live(tmp_path, "alertmanager.yml.tpl", "old\n")
    live(tmp_path, "compose.yml", "services: {}\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    installer.install_alertmanager_template(ctx, ctx.env["ALERTMANAGER_DEST"])

    assert dest.read_text(encoding="utf-8") == TEMPLATE
    assert fake.ran("compose")
    assert fake.ran("exec")


def test_an_unchanged_template_does_not_recreate_alertmanager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recreate drops the in-memory state of every active alert and would
    re-fire pending ones on every deploy."""
    installer = load_installer()
    fake = FakeDocker()
    monkeypatch.setattr(installer, "_run", fake)
    stage(tmp_path, "alertmanager.yml.tpl", TEMPLATE)
    live(tmp_path, "alertmanager.yml.tpl", TEMPLATE)
    live(tmp_path, "compose.yml", "services: {}\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    installer.install_alertmanager_template(ctx, ctx.env["ALERTMANAGER_DEST"])

    assert not fake.ran("compose")


def test_a_rejected_template_is_never_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker(amtool=1))
    stage(tmp_path, "alertmanager.yml.tpl", TEMPLATE)
    dest = live(tmp_path, "alertmanager.yml.tpl", "old\n")
    live(tmp_path, "compose.yml", "services: {}\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    with pytest.raises(InstallError, match="amtool rejected"):
        installer.install_alertmanager_template(ctx, ctx.env["ALERTMANAGER_DEST"])

    assert dest.read_text(encoding="utf-8") == "old\n"


def test_a_recreated_alertmanager_that_renders_a_bad_config_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker(**{"exec": 1}))
    stage(tmp_path, "alertmanager.yml.tpl", TEMPLATE)
    live(tmp_path, "alertmanager.yml.tpl", "old\n")
    live(tmp_path, "compose.yml", "services: {}\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    with pytest.raises(InstallError, match="rendered an invalid config"):
        installer.install_alertmanager_template(ctx, ctx.env["ALERTMANAGER_DEST"])


def test_a_failed_recreate_fails_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    monkeypatch.setattr(installer, "_run", FakeDocker(compose=1))
    stage(tmp_path, "alertmanager.yml.tpl", TEMPLATE)
    live(tmp_path, "alertmanager.yml.tpl", "old\n")
    live(tmp_path, "compose.yml", "services: {}\n")
    ctx = make_ctx(tmp_path, vmagent_env(tmp_path))

    with pytest.raises(InstallError, match="failed to recreate alertmanager"):
        installer.install_alertmanager_template(ctx, ctx.env["ALERTMANAGER_DEST"])


def test_compose_output_is_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator reads the recreate as it happens; capturing it would hold
    every line until the container was already back up."""
    installer = load_installer()
    captured: list[bool] = []

    def record(command, **kwargs):
        captured.append(bool(kwargs.get("capture_output")))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(installer, "_run", record)
    installer.recreate_alertmanager(live(tmp_path, "compose.yml", "services: {}\n"))

    assert captured == [False, True]
