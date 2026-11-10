from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from homelab.hosts import default_registry
from homelab.modules import monitoring_config
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError


def write_module_files(root: Path) -> None:
    configs_dir = root / "monitoring-config" / "configs"
    configs_dir.mkdir(parents=True)
    templates_dir = root / "monitoring-config" / "templates"
    templates_dir.mkdir()
    (templates_dir / "scrape.yml.j2").write_text("scrape_configs: []\n", encoding="utf-8")
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


def test_validate_requires_the_scrape_template(tmp_path: Path) -> None:
    write_module_files(tmp_path)
    (tmp_path / "monitoring-config" / "templates" / "scrape.yml.j2").unlink()

    with pytest.raises(ValueError, match=r"missing required file: .*scrape\.yml\.j2"):
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
    """scrape.yml is rendered per host into build/; everything else is static configs/."""
    subdir = "build" if name == "scrape.yml" else "configs"
    path = tmp_path / subdir / name
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


def test_template_regex_repeat_counts_fit_go_regexp() -> None:
    """Go's regexp rejects a repeat count over 1000, and only at render time:
    amtool config check passes, and Alertmanager sends whatever rendered before
    the failing call. A 3500-character cap delivered the probe report as a bare
    header.
    """
    configs = Path(__file__).resolve().parents[1] / "monitoring-config" / "configs"
    template = (configs / "alertmanager.yml.tpl").read_text(encoding="utf-8")
    counts = [int(n) for n in re.findall(r"\{(\d+)(?:,\d*)?\}", template)]
    assert counts, "no regex repeat counts found; the pattern no longer matches the template"
    too_big = [n for n in counts if n > 1000]
    assert not too_big, f"repeat count(s) over Go's 1000 limit: {too_big}"


# ---------------------------------------------------------------------------
# Scrape-time drops vs. the rules that read them (freender/homelab-ops#359)
#
# A metric_relabel_configs drop is silent: a rule on a dropped metric evaluates
# to nothing and never fires. These tests replay the real scrape.yml drops
# against every metric name the real vmalert rules mention.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = REPO_ROOT / "vmalert-rules" / "configs"

# A keep-list only answers for its own exporter's families: cadvisor dropping
# node_* says nothing, because cadvisor never exports node_*.
KEEP_LIST_PREFIXES = {
    "cadvisor": "container_",
    "home-assistant": "homeassistant_",
    "vector": "vector_",
}

_SELECTOR = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{([^}]*)\}")
_EQUALITY_MATCHER = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_IDENTIFIER = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


def scrape_jobs() -> list[dict]:
    rendered = monitoring_config.render_scrape_config(REPO_ROOT)
    return yaml.safe_load(rendered)["scrape_configs"]


def survives(relabels: list[dict], labels: dict[str, str]) -> bool:
    """vmagent's keep/drop semantics: anchored regex over ';'-joined labels."""
    for relabel in relabels:
        joined = relabel.get("separator", ";").join(
            labels.get(name, "") for name in relabel["source_labels"]
        )
        hit = re.fullmatch(relabel["regex"], joined) is not None
        if relabel["action"] == "drop" and hit:
            return False
        if relabel["action"] == "keep" and not hit:
            return False
    return True


def referenced_series(expr: str) -> list[tuple[str, dict[str, str]]]:
    """Every name{...} selector with its equality matchers, plus bare names.

    Function names, keywords and label names come along as "bare names" too.
    That is harmless: drops here are exact names and keep-lists are checked
    only against their own prefix, so a stray `rate` or `host` never matches.
    """
    found = [
        (name, dict(_EQUALITY_MATCHER.findall(matchers)))
        for name, matchers in _SELECTOR.findall(expr)
    ]
    rest = re.sub(r'"[^"]*"', " ", _SELECTOR.sub(" ", expr))
    found.extend((name, {}) for name in _IDENTIFIER.findall(rest))
    return found


def rule_exprs() -> list[tuple[str, str]]:
    exprs = []
    for rule_file in sorted(RULES_DIR.glob("*.yml")):
        for group in yaml.safe_load(rule_file.read_text(encoding="utf-8"))["groups"]:
            exprs.extend((rule_file.name, rule["expr"]) for rule in group["rules"])
    return exprs


def test_relabel_rules_use_only_modelled_actions() -> None:
    """survives() models keep and drop only, so anything else would go unchecked."""
    for job in scrape_jobs():
        relabels = job.get("metric_relabel_configs", [])
        assert all(r["action"] in ("keep", "drop") for r in relabels), job["job_name"]
        if any(r["action"] == "keep" for r in relabels):
            assert job["job_name"] in KEEP_LIST_PREFIXES, (
                f"{job['job_name']} has a keep-list; add its family prefix to KEEP_LIST_PREFIXES"
            )


def test_the_drops_really_drop() -> None:
    """Keeps the guard below from passing vacuously on a regex that matches nothing."""
    jobs = {job["job_name"]: job.get("metric_relabel_configs", []) for job in scrape_jobs()}

    systemd = {"__name__": "node_systemd_unit_state"}
    assert not survives(jobs["pve-node"], {**systemd, "state": "active"})
    assert survives(jobs["pve-node"], {**systemd, "state": "failed"})
    assert not survives(jobs["cadvisor"], {"__name__": "container_fs_usage_bytes"})
    assert not survives(jobs["home-assistant"], {"__name__": "homeassistant_entity_available"})
    assert not survives(jobs["vmagent"], {"__name__": "flag"})
    assert not survives(jobs["vector"], {"__name__": "vector_component_latency_seconds_bucket"})
    assert survives(jobs["vector"], {"__name__": "vector_http_client_errors_total"})


def test_no_vmalert_rule_reads_a_metric_the_scrape_config_drops() -> None:
    exprs = rule_exprs()
    assert len(exprs) > 50, "rule discovery found too few rules; the glob or layout changed"

    dropped = []
    for job in scrape_jobs():
        relabels = job.get("metric_relabel_configs", [])
        if not relabels:
            continue
        prefix = KEEP_LIST_PREFIXES.get(job["job_name"])
        for rule_file, expr in exprs:
            for name, labels in referenced_series(expr):
                if prefix is not None and not name.startswith(prefix):
                    continue
                if not survives(relabels, {"__name__": name, **labels}):
                    dropped.append(f"{rule_file}: {name}{labels or ''} by {job['job_name']}")

    assert not dropped, "\n".join(sorted(set(dropped)))


# ---------------------------------------------------------------------------
# Host-derived scrape targets (freender/homelab-ops#379)
#
# pve-node, pve-smartctl, apcupsd and intel-gpu are rendered from hosts.conf so
# a host that runs an exporter cannot go unscraped. The labels are what vmalert
# rules and Grafana panels select on, so they are pinned exactly.
# ---------------------------------------------------------------------------

HOST_DERIVED_JOBS = ("pve-node", "pve-smartctl", "apcupsd", "intel-gpu")


def rendered_targets(root: Path) -> dict[str, list[tuple[str, dict]]]:
    jobs = yaml.safe_load(monitoring_config.render_scrape_config(root))["scrape_configs"]
    return {
        job["job_name"]: [
            (target, static.get("labels") or {})
            for static in job.get("static_configs") or []
            for target in static["targets"]
        ]
        for job in jobs
    }


def test_the_real_inventory_renders_the_expected_targets() -> None:
    """Golden: what today's hosts.conf must render. Equal to the hand-written file
    this replaced plus glint, which ran node_exporter unscraped until #379."""
    jobs = rendered_targets(REPO_ROOT)

    def by_host(job: str) -> dict[str, str]:
        return {labels["host"]: target for target, labels in jobs[job]}

    lan = "freender.internal"
    assert by_host("pve-node") == {
        **{h: f"{h}.{lan}:9100" for h in (
            "ace", "bray", "clovis", "osiris", "arc", "xur", "deepstone", "neo",
            "tower", "riven", "helm", "glint", "ghost",
        )},
        "cottonwood": "cottonwood.internal:9100",
        "cinci": "cinci.internal:9100",
    }
    assert by_host("pve-smartctl") == {
        **{h: f"{h}.{lan}:9633" for h in ("ace", "bray", "clovis", "osiris")},
        "cottonwood": "cottonwood.internal:9633",
        "cinci": "cinci.internal:9633",
    }
    assert by_host("apcupsd") == {h: f"{h}.{lan}:9162" for h in ("bray", "osiris")}
    assert by_host("intel-gpu") == {h: f"{h}.{lan}:9400" for h in ("ace", "bray", "clovis")}
    vector = {labels["host"]: (target, labels) for target, labels in jobs["vector"]}
    assert {h: t for h, (t, _labels) in vector.items()} == {
        "tower": "vector-tower:9598",
        "helm": "vector-helm:9598",
        "neo": "vector-neo:9598",
        **{h: f"{h}.{lan}:9598" for h in (
            "ace", "bray", "clovis", "osiris", "arc", "xur", "riven",
        )},
    }


def test_only_host_native_vector_targets_are_marked_install_host() -> None:
    """VectorDockerSourceDead skips install="host": those have no docker_logs and
    would otherwise fire on every one of them."""
    for target, labels in rendered_targets(REPO_ROOT)["vector"]:
        expected = {"host"} if target.startswith("vector-") else {"host", "install"}
        assert set(labels) == expected, target
        assert labels.get("install", "host") == "host"
    rules = (REPO_ROOT / "vmalert-rules/configs/monitoring-stack.yml").read_text(encoding="utf-8")
    assert 'up{job="vector", install!="host"} == 1 unless' in rules


def test_every_metrics_exporters_host_has_exactly_one_node_target() -> None:
    """The issue's minimum guarantee, kept as a guard against a template edit
    that drops or duplicates the loop."""
    hosts = default_registry(REPO_ROOT).list_hosts("metrics-exporters")
    scraped = [labels["host"] for _, labels in rendered_targets(REPO_ROOT)["pve-node"]]
    assert sorted(scraped) == sorted(hosts)


def test_host_derived_targets_carry_only_the_host_label() -> None:
    """endpoint and instance come from relabel_configs; any extra static label
    would add a dimension every rule and panel would have to aggregate away."""
    jobs = rendered_targets(REPO_ROOT)
    for job in HOST_DERIVED_JOBS:
        assert jobs[job], f"{job} rendered no targets"
        assert all(set(labels) == {"host"} for _, labels in jobs[job]), job


def write_inventory(root: Path, hosts: dict) -> None:
    (root / "hosts.conf").write_text(yaml.safe_dump(hosts, sort_keys=False), encoding="utf-8")


def inventory_host(hostname: str, **features) -> dict:
    config = {"hostname": hostname, "user": "root", "sshkey": "infra", "type": "ubuntu"}
    return {"config": config, "features": features}


def test_scrape_targets_follow_the_exporter_flags(tmp_path: Path) -> None:
    write_inventory(
        tmp_path,
        {
            "metal": inventory_host(
                "metal.lan",
                **{
                    "metrics-exporters": {"intel_gpu_exporter": {"x": 1}},
                    "apcupsd": {"role": "master"},
                },
            ),
            "ups-slave": inventory_host(
                "slave.lan", **{"metrics-exporters": None, "apcupsd": {"role": "slave"}}
            ),
            "guest": inventory_host(
                "10.0.0.9",
                **{"metrics-exporters": {"lxc_guest": True, "scrape_host": "guest.lan"}},
            ),
            "gated": inventory_host("gated.lan", **{"metrics-exporters": {"deploy": False}}),
            "bare": inventory_host("bare.lan"),
            "shipper": inventory_host("10.0.0.7", **{
                "metrics-exporters": {"lxc_guest": True, "scrape_host": "shipper.lan"},
                "vector-host": {"shadow": True},
            }),
            "plain-shipper": inventory_host("plain.lan", **{"vector-host": None}),
        },
    )

    targets = {
        job: [(t.host, t.address) for t in entries]
        for job, entries in monitoring_config.scrape_targets(tmp_path).items()
    }

    assert targets == {
        "pve-node": [
            ("metal", "metal.lan:9100"),
            ("ups-slave", "slave.lan:9100"),
            ("guest", "guest.lan:9100"),
            ("shipper", "shipper.lan:9100"),
        ],
        "pve-smartctl": [("metal", "metal.lan:9633"), ("ups-slave", "slave.lan:9633")],
        "apcupsd": [("metal", "metal.lan:9162")],
        "intel-gpu": [("metal", "metal.lan:9400")],
        "vector-host": [("shipper", "shipper.lan:9598"), ("plain-shipper", "plain.lan:9598")],
    }


def test_deploy_host_renders_the_scrape_config_into_the_build_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer reads build/<host>/scrape.yml; configs/ no longer has one."""
    write_module_files(tmp_path)
    (tmp_path / "monitoring-config" / "templates" / "scrape.yml.j2").write_text(
        "{% for t in targets['pve-node'] %}{{ t.host }}={{ t.address }}\n{% endfor %}",
        encoding="utf-8",
    )
    write_inventory(tmp_path, {"helm": inventory_host("helm.lan", **{"metrics-exporters": None})})
    diffed: list = []
    monkeypatch.setattr(monitoring_config, "connection_for_host", lambda root, host: None)
    monkeypatch.setattr(
        monitoring_config, "diff_many", lambda conn, pairs: diffed.extend(pairs) or []
    )

    monitoring_config.deploy_host(tmp_path, "helm", dry_run=True, force=False)

    rendered = tmp_path / "monitoring-config" / "build" / "helm" / "scrape.yml"
    assert rendered.read_text(encoding="utf-8") == "helm=helm.lan:9100\n"
    assert diffed == [(rendered, monitoring_config.REMOTE_SCRAPE_CONFIG)]
