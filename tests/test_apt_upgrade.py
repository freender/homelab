"""Guards for apt-upgrade's opt-in unattended reboot.

auto_reboot hands the reboot decision to unattended-upgrades rather than
reimplementing it, so the risk is not in the mechanism -- it is in the flag
reaching a host that must never reboot itself. These tests pin the default to
false, pin the live inventory to the two hosts that opted in, and pin the
generated apt config to the keys u-u actually reads.
"""

from __future__ import annotations

import contextlib
import importlib.util
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from homelab.hosts import default_registry
from homelab.modules import apt_upgrade
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "apt-upgrade" / "scripts" / "install.py"


# ---------------------------------------------------------------------------
# Loading the remote installer in-process.
#
# It is a script rather than a package module, so it is loaded by path. A fresh
# load per test on purpose: these tests rebind module-level constants like
# AUTO_REBOOT_PATH to a tmp_path, and a cached module would leak that into the
# next test as a path that no longer exists.
# ---------------------------------------------------------------------------


def load_installer():
    spec = importlib.util.spec_from_file_location("apt_upgrade_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_ctx(
    tmp_path: Path,
    env: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
    file_map: dict[str, tuple[str, str]] | None = None,
) -> InstallContext:
    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        (build_dir / name).write_text(content, encoding="utf-8")
    return InstallContext(
        host="testhost",
        script_dir=tmp_path,
        build_dir=build_dir,
        env=env or {},
        deploy_env={},
        file_map=file_map or {},
        force_update=False,
    )


@contextlib.contextmanager
def fake_commands(
    module, outputs: dict[tuple[str, ...], str], failing: tuple[tuple[str, ...], ...] = ()
) -> Iterator[list[list[str]]]:
    """Replace a module's `_run` indirection point. Matches on argv prefix, so a
    test can pin `apt-config` without caring what else the installer shells out to."""
    seen: list[list[str]] = []

    def fake_run(command, **_kwargs):
        seen.append(list(command))
        for prefix, out in outputs.items():
            if tuple(command[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")
        for prefix in failing:
            if tuple(command[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    original = module._run
    module._run = fake_run
    try:
        yield seen
    finally:
        module._run = original


@contextlib.contextmanager
def installed_packages(installer, present: tuple[str, ...]) -> Iterator[None]:
    original = installer.packages.installed
    installer.packages.installed = lambda _ctx, name: name in present
    try:
        yield
    finally:
        installer.packages.installed = original

# Hosts that carry an HA role or a singleton the rest of the homelab depends on:
# tower is primary keepalived/Traefik and the media/storage host, helm is the
# whole monitoring stack, neo is tertiary keepalived, riven is the OpenCode
# server. A reboot flag landing on any of these is the regression to catch.
#
# The four PVE nodes joined this list when they moved onto this module. They are
# the most important entries: their guests are all LXC and cannot live-migrate,
# so with ha shutdown_policy=migrate an unattended reboot restarts every guest
# on the node. arc and xur are here for a different reason -- both are LXC
# guests on bray's kernel, so a reboot of *them* is meaningless, but xur is the
# primary PBS and arc is the PDM host, and neither should ever acquire the flag
# by a copy-paste from an offsite block.
MUST_NEVER_AUTO_REBOOT = (
    "tower",
    "helm",
    "neo",
    "riven",
    "ace",
    "bray",
    "clovis",
    "osiris",
    "arc",
    "xur",
)

HOSTS_TEMPLATE = """\
{host}:
  config:
    type: ubuntu
    hostname: {host}.internal
    user: root
    sshkey: infra
  features:
    apt-upgrade:
      autoupgrade: true
{extra}"""


def registry_for(tmp_path: Path, host: str, extra: str = "") -> object:
    (tmp_path / "hosts.conf").write_text(
        HOSTS_TEMPLATE.format(host=host, extra=extra), encoding="utf-8"
    )
    return default_registry(tmp_path)


def test_auto_reboot_defaults_to_false(tmp_path: Path) -> None:
    registry = registry_for(tmp_path, "somehost")
    assert apt_upgrade.normalize_auto_reboot(registry, "somehost") is False


def test_auto_reboot_reads_the_flag(tmp_path: Path) -> None:
    registry = registry_for(tmp_path, "somehost", extra="      auto_reboot: true\n")
    assert apt_upgrade.normalize_auto_reboot(registry, "somehost") is True


def test_auto_reboot_rejects_a_non_boolean(tmp_path: Path) -> None:
    registry = registry_for(tmp_path, "somehost", extra="      auto_reboot: sometimes\n")
    with pytest.raises(ValueError, match="auto_reboot must be true or false"):
        apt_upgrade.normalize_auto_reboot(registry, "somehost")


def test_live_inventory_only_auto_reboots_the_offsite_hosts() -> None:
    """cottonwood and cinci opted in; nothing load-bearing may join them."""
    registry = default_registry(ROOT)
    enabled = [
        host
        for host in registry.list_hosts(feature="apt-upgrade")
        if apt_upgrade.normalize_auto_reboot(registry, host)
    ]
    assert sorted(enabled) == ["cinci", "cottonwood"]


def test_ha_and_singleton_hosts_never_auto_reboot() -> None:
    registry = default_registry(ROOT)
    for host in MUST_NEVER_AUTO_REBOOT:
        assert apt_upgrade.normalize_auto_reboot(registry, host) is False, host


def test_every_enabled_host_has_a_supported_type() -> None:
    """A host declaring apt-upgrade but skipped by the type gate is a silent no-op.

    deploy_host() prints a skip and returns 0, so an unsupported type does not
    fail the deploy -- the host simply never gets the timer while hosts.conf
    claims it does. That is exactly the drift this module exists to remove, so
    pin the two sides together against live inventory.
    """
    registry = default_registry(ROOT)
    enabled = registry.list_hosts(feature="apt-upgrade")
    assert enabled, "no host enables apt-upgrade; the gate test is vacuous"
    for host in enabled:
        host_type = registry.get(host, "config.type")
        assert host_type in apt_upgrade.SUPPORTED_TYPES, f"{host}: {host_type}"


def test_pve_nodes_are_scheduled_before_the_saturday_reboot_digest() -> None:
    """The 05:00-05:15 band is coupled to RebootRequired's 1h `for:`.

    A kernel installed on a PVE node must cross that threshold before the
    Saturday 09:00-09:10 Alertmanager window, or the prompt to reboot is held
    for a further week. The exporter refreshes every 15m, so the run must
    finish comfortably before 08:00. Moving these later without also moving the
    alert is the regression.
    """
    registry = default_registry(ROOT)
    for host in ("ace", "bray", "clovis", "osiris"):
        schedule = str(registry.get(host, "apt-upgrade.schedule"))
        hour = int(schedule.rsplit(" ", 1)[1].split(":")[0])
        assert 3 <= hour <= 6, f"{host} runs at {schedule}, too close to the 09:00 digest"


def test_generated_conf_sets_the_keys_unattended_upgrades_reads(tmp_path: Path) -> None:
    apt_upgrade.write_auto_reboot_conf(tmp_path, apt_upgrade.DEFAULT_AUTO_REBOOT_TIME)
    text = (tmp_path / "auto-reboot.conf").read_text(encoding="utf-8")

    assert 'Unattended-Upgrade::Automatic-Reboot "true";' in text
    assert 'Unattended-Upgrade::Automatic-Reboot-WithUsers "true";' in text
    # "now" means "when the u-u run finishes". A clock time here would be
    # scheduled with `shutdown -r <time>` and roll to the next day whenever the
    # run overshoots it, racing apt-daily-upgrade.timer's randomised window.
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "now";' in text


def test_generated_conf_honours_a_custom_time(tmp_path: Path) -> None:
    apt_upgrade.write_auto_reboot_conf(tmp_path, "04:00")
    text = (tmp_path / "auto-reboot.conf").read_text(encoding="utf-8")
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "04:00";' in text


def test_env_carries_auto_reboot_to_the_installer(tmp_path: Path) -> None:
    apt_upgrade.write_env(
        tmp_path, autoupgrade="true", schedule="*-*-* 08:00:00", paused=False, auto_reboot=True
    )
    assert "AUTO_REBOOT=true" in (tmp_path / "env").read_text(encoding="utf-8")

    apt_upgrade.write_env(tmp_path, autoupgrade="true", schedule="*-*-* 08:00:00", paused=False)
    assert "AUTO_REBOOT=false" in (tmp_path / "env").read_text(encoding="utf-8")


def test_installer_removes_the_drop_in_when_disabled(tmp_path: Path) -> None:
    """The flag must be reversible: no drop-in left behind when it is taken away.

    Behavioural now that the installer is Python. The bash version of this test
    could only match source text, which would have passed just as happily on a
    `rm -f` that ran in an unreachable branch.
    """
    installer = load_installer()
    dropin = tmp_path / "53homelab-auto-reboot"
    dropin.write_text("Unattended-Upgrade::Automatic-Reboot \"true\";\n", encoding="utf-8")

    ctx = make_ctx(tmp_path)
    installer.AUTO_REBOOT_PATH = str(dropin)
    installer.apply_auto_reboot(ctx, auto_reboot=False)

    assert not dropin.exists()


def test_installer_leaves_an_absent_drop_in_alone(tmp_path: Path) -> None:
    """Removal has to be idempotent -- this runs on every deploy to every host,
    and the overwhelmingly common case is that there is nothing to remove."""
    installer = load_installer()
    installer.AUTO_REBOOT_PATH = str(tmp_path / "never-existed")

    installer.apply_auto_reboot(make_ctx(tmp_path), auto_reboot=False)


def test_installer_refuses_auto_reboot_without_unattended_upgrades(tmp_path: Path) -> None:
    """It supplies the reboot mechanism; installing it silently would change the
    host's upgrade behaviour as a side effect of setting a reboot flag."""
    installer = load_installer()
    ctx = make_ctx(tmp_path)

    with pytest.raises(InstallError, match="requires unattended-upgrades"):
        with installed_packages(installer, present=()):
            installer.apply_auto_reboot(ctx, auto_reboot=True)


def test_installer_verifies_the_resolved_policy_not_the_file_it_wrote(tmp_path: Path) -> None:
    """APT merges all of apt.conf.d in order, so a later fragment can still win.
    Checking the file we just wrote would only confirm we wrote it."""
    installer = load_installer()
    installer.AUTO_REBOOT_PATH = str(tmp_path / "dropin")
    ctx = make_ctx(
        tmp_path,
        files={"auto-reboot.conf": "// managed\n"},
        file_map={"auto-reboot.conf": (str(tmp_path / "dropin"), "644")},
    )

    with installed_packages(installer, present=("unattended-upgrades",)):
        overridden = 'Unattended-Upgrade::Automatic-Reboot "";'
        with fake_commands(installer, {("apt-config",): overridden}):
            with pytest.raises(InstallError, match="Automatic-Reboot is not true"):
                installer.apply_auto_reboot(ctx, auto_reboot=True)


def test_installer_requires_the_timer_that_would_actually_reboot(tmp_path: Path) -> None:
    """auto_reboot only ever fires at the end of an unattended-upgrades run."""
    installer = load_installer()
    installer.AUTO_REBOOT_PATH = str(tmp_path / "dropin")
    ctx = make_ctx(
        tmp_path,
        files={"auto-reboot.conf": "// managed\n"},
        file_map={"auto-reboot.conf": (str(tmp_path / "dropin"), "644")},
    )

    with installed_packages(installer, present=("unattended-upgrades",)):
        with fake_commands(
            installer,
            {("apt-config",): 'Unattended-Upgrade::Automatic-Reboot "true";'},
            failing=(("systemctl", "is-enabled"),),
        ):
            with pytest.raises(InstallError, match="apt-daily-upgrade.timer is not enabled"):
                installer.apply_auto_reboot(ctx, auto_reboot=True)


def test_pause_stops_the_host_rebooting_itself(tmp_path: Path) -> None:
    """Pause means the host stops acting on its own, and rebooting itself is
    acting on its own. This is the assertion the bash test approximated by
    grepping for `AUTO_REBOOT="false"` next to the call."""
    installer = load_installer()
    dropin = tmp_path / "53homelab-auto-reboot"
    dropin.write_text("Unattended-Upgrade::Automatic-Reboot \"true\";\n", encoding="utf-8")
    installer.AUTO_REBOOT_PATH = str(dropin)

    ctx = make_ctx(
        tmp_path,
        env={"AUTOUPGRADE": "true", "PAUSED": "true", "AUTO_REBOOT": "true"},
        files={"service": "[Unit]\n"},
        file_map={"service": (str(tmp_path / "svc"), "644")},
    )

    with fake_commands(installer.systemd, {}):
        installer.install(ctx)

    assert not dropin.exists(), "a paused host must not keep the reboot drop-in"


# ---------------------------------------------------------------------------
# stage_and_install: which files reach the host.
#
# deploy_host decides *whether* auto-reboot.conf is written; this decides
# whether it is uploaded. A conf built into build/ but left out of the upload
# list would make the flag a no-op with a fully successful deploy, so the
# file-map is asserted directly rather than inferred from a dry run.
# ---------------------------------------------------------------------------


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture stage_and_run_remote_installer's arguments instead of connecting."""
    calls: list[dict] = []

    def record(root, connection, remote_root, upload_paths, installer, *args, **kwargs):
        calls.append(
            {
                "root": root,
                "connection": connection,
                "remote_root": remote_root,
                "upload_paths": upload_paths,
                "installer": installer,
                "args": args,
                **kwargs,
            }
        )

    monkeypatch.setattr(apt_upgrade, "stage_and_run_remote_installer", record)
    return calls


def _build_dir(tmp_path: Path, *names: str) -> Path:
    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (build_dir / name).write_text(f"{name}\n", encoding="utf-8")
    return build_dir


def test_stage_uploads_the_scripts_dir_and_the_per_host_build_dir(
    tmp_path: Path, staged: list[dict]
) -> None:
    """`homelab_install.run()` resolves `build/<host>/`, so the directory is
    staged whole rather than as a hand-maintained list of filenames. That list
    was a second place the set of built files had to be kept in step."""
    build_dir = _build_dir(tmp_path, "service", "env", "timer", "auto-reboot.conf")

    apt_upgrade.stage_and_install(tmp_path, build_dir, connection=object(), host="ace", force=False)

    assert [remote for _local, remote in staged[0]["upload_paths"]] == [
        f"{apt_upgrade.REMOTE_ROOT}/scripts",
        f"{apt_upgrade.REMOTE_ROOT}/build/ace",
    ]
    assert staged[0]["upload_paths"][0][0] == tmp_path / "apt-upgrade" / "scripts"
    assert staged[0]["upload_paths"][1][0] == build_dir


def test_stage_passes_the_host_so_the_installer_finds_its_build_dir(
    tmp_path: Path, staged: list[dict]
) -> None:
    """`run()` defaults the host to `socket.gethostname()`, which is not the
    inventory name for every host -- deepstone answers to `timemachine`. Left to
    the default it would look for a build directory that was never staged."""
    apt_upgrade.stage_and_install(
        tmp_path, _build_dir(tmp_path, "env"), connection=object(), host="deepstone", force=False
    )

    assert staged[0]["args"] == ("deepstone",)


def test_stage_requires_root_and_makes_the_three_remote_subdirs(
    tmp_path: Path, staged: list[dict]
) -> None:
    apt_upgrade.stage_and_install(
        tmp_path, _build_dir(tmp_path, "env"), connection=object(), host="ace", force=False
    )

    call = staged[0]
    assert call["installer"] == apt_upgrade.INSTALLER
    assert call["interpreter"] == apt_upgrade.INTERPRETER
    assert call["require_root"] is True  # the installer writes to /etc and systemd
    assert call["remote_subdirs"] == ("build", "lib", "scripts")


def test_stage_passes_force_through_to_the_installer_env(
    tmp_path: Path, staged: list[dict]
) -> None:
    build_dir = _build_dir(tmp_path, "env")

    apt_upgrade.stage_and_install(tmp_path, build_dir, connection=object(), host="ace", force=True)
    apt_upgrade.stage_and_install(tmp_path, build_dir, connection=object(), host="ace", force=False)

    assert staged[0]["env"] == apt_upgrade.force_env(True)
    assert staged[1]["env"] == apt_upgrade.force_env(False)


def test_the_file_map_only_lists_files_this_host_actually_gets(tmp_path: Path) -> None:
    """A map entry with no build file behind it makes `files.install` raise on a
    missing source, so the conditional render and the conditional map entry have
    to stay in step. This is where the old "is it uploaded?" guard now lives."""
    build_dir = tmp_path / "build" / "ace"

    apt_upgrade.build_unit_files(
        build_dir,
        autoupgrade="false",
        schedule="*-*-* 09:00:00",
        paused=False,
        auto_reboot=False,
        auto_reboot_time="now",
    )

    mapped = (build_dir / "file-map.conf").read_text(encoding="utf-8")
    assert "auto-reboot.conf" not in mapped, "a flag deploy_host declined to set"
    assert "timer" not in mapped
    for name in [line.split("|")[0] for line in mapped.splitlines() if line.strip()]:
        assert (build_dir / name).is_file(), f"{name} is mapped but was never rendered"


def test_the_file_map_gains_the_timer_and_drop_in_when_both_are_on(tmp_path: Path) -> None:
    build_dir = tmp_path / "build" / "cinci"

    apt_upgrade.build_unit_files(
        build_dir,
        autoupgrade="true",
        schedule="*-*-* 09:00:00",
        paused=False,
        auto_reboot=True,
        auto_reboot_time="now",
    )

    mapped = (build_dir / "file-map.conf").read_text(encoding="utf-8")
    assert f"/etc/systemd/system/{apt_upgrade.TIMER_NAME}" in mapped
    assert apt_upgrade.AUTO_REBOOT_PATH in mapped
    for name in [line.split("|")[0] for line in mapped.splitlines() if line.strip()]:
        assert (build_dir / name).is_file(), f"{name} is mapped but was never rendered"
