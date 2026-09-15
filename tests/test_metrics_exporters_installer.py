"""Remote installer for metrics-exporters (freender/homelab-ops#30).

Every absolute destination is rebound under `tmp_path`, and the subprocess surfaces
-- `packages._run` (dpkg/apt), `systemd._run` (systemctl), and the installer's own
`_run` (systemd-detect-virt) -- are faked. Nothing here touches `/etc`, a real unit,
or apt.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.modules import metrics_exporters
from homelab_install import packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "metrics-exporters" / "scripts" / "install.py"
REBINDS = (
    "OS_RELEASE",
    "DEV_ZFS",
    "SYSTEMD_DIR",
    "BIN_DIR",
    "DEFAULT_DIR",
    "ETC_HOMELAB",
    "TEXTFILE_DIR",
    "BACKPORTS_SOURCE",
)
DEBIAN = 'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'
UBUNTU = 'ID=ubuntu\nVERSION_ID="26.04"\nVERSION_CODENAME=resolute\n'
NODE = "prometheus-node-exporter"
SMART = "prometheus-smartctl-exporter"
NODE_UNIT = f"{NODE}.service"


def load_installer():
    """Fresh load per test: these rebind the module's destination constants."""
    spec = importlib.util.spec_from_file_location("metrics_exporters_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def specs(**flags: bool):
    options = {
        "has_apcupsd": False,
        "has_igpu": False,
        "has_hba": False,
        "has_expected_pools": False,
        "has_disk_label_overrides": False,
        "has_pve_patch_statuses": False,
        "has_wrapper": False,
        "lxc_guest": False,
    }
    options.update(flags)
    return metrics_exporters.build_file_specs(**options)


def everything():
    return specs(
        has_apcupsd=True,
        has_igpu=True,
        has_hba=True,
        has_expected_pools=True,
        has_disk_label_overrides=True,
        has_pve_patch_statuses=True,
        has_wrapper=True,
    )


class FakeApt:
    def __init__(self, missing: set[str] | None = None) -> None:
        self.missing = set(missing or ())
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[0] == "dpkg-query":
            ok = command[-1] not in self.missing
            return subprocess.CompletedProcess(
                command, 0 if ok else 1, stdout="install ok installed"
            )
        if command[:2] == ["apt-get", "install"]:
            self.missing -= set(command)
        return subprocess.CompletedProcess(command, 0)

    @property
    def apt_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "apt-get"]


# `reset-failed` is left out as well: `systemd.retire_unit` issues it for every
# unconfigured exporter on every run, and it changes nothing on a unit with no
# failed record.
QUERIES = {"is-enabled", "is-active", "is-failed", "list-unit-files", "reset-failed"}


class FakeSystemctl:
    def __init__(self) -> None:
        self.enabled: set[str] = set()
        self.active: set[str] = set()
        self.masked: set[str] = set()
        self.installed: set[str] = set()
        self.dead: set[str] = set()  # units that never come up
        self.fail_start: set[str] = set()
        self.calls: list[list[str]] = []

    def up(self, *units: str) -> None:
        self.enabled.update(units)
        self.active.update(units)

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        verb, unit = command[1], command[-1]
        if verb == "is-enabled":
            if "--quiet" not in command:
                state = "masked" if unit in self.masked else "enabled"
                return subprocess.CompletedProcess(command, 0, stdout=f"{state}\n")
            return subprocess.CompletedProcess(command, 0 if unit in self.enabled else 1)
        if verb == "is-active":
            return subprocess.CompletedProcess(command, 0 if unit in self.active else 3)
        if verb == "list-unit-files":
            return subprocess.CompletedProcess(command, 0 if unit in self.installed else 1)
        if verb in {"enable", "restart", "start"}:
            if verb == "start" and unit in self.fail_start:
                return subprocess.CompletedProcess(command, 1)
            if unit not in self.dead:
                self.up(unit)
        if verb == "disable":
            self.enabled.discard(unit)
            self.active.discard(unit)
        if verb == "mask":
            self.masked.add(unit)
        return subprocess.CompletedProcess(command, 0)

    def actions(self) -> list[str]:
        return [" ".join(call[1:]) for call in self.calls if call[1] not in QUERIES]


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.sandbox = tmp_path / "hostfs"
        self.installer = load_installer()
        for name in REBINDS:
            setattr(self.installer, name, self.under(getattr(self.installer, name)))

        self.build_dir = tmp_path / "remote" / "build" / "host"
        self.build_dir.mkdir(parents=True)
        self.file_map: dict[str, tuple[str, str]] = {}
        self.use(specs())
        self.os_release(DEBIAN)
        Path(self.installer.DEV_ZFS).parent.mkdir(parents=True, exist_ok=True)
        Path(self.installer.DEV_ZFS).touch()

        self.apt = FakeApt()
        self.systemctl = FakeSystemctl()
        self.systemctl.up(NODE_UNIT, "smartctl_exporter.service")
        self.container = False
        self.force = False

    def under(self, dest: str) -> str:
        return str(self.sandbox / dest.lstrip("/"))

    def os_release(self, text: str) -> None:
        path = Path(self.installer.OS_RELEASE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def use(self, file_specs) -> None:
        self.file_map = {}
        for spec in file_specs:
            staged = self.build_dir / spec.build_name
            if not staged.exists():
                staged.write_text(f"# {spec.build_name}\n", encoding="utf-8")
            self.file_map[spec.build_name] = (self.under(spec.remote_path), spec.mode)

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def seed(self, dest: str, text: str = "stale\n") -> Path:
        path = Path(self.under(dest))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _host(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        assert command[0] == "systemd-detect-virt", command
        return subprocess.CompletedProcess(command, 0 if self.container else 1)

    def run(self) -> InstallContext:
        self.monkeypatch.setattr(packages, "_run", self.apt)
        self.monkeypatch.setattr(packages, "_apt_updated", False)
        self.monkeypatch.setattr(systemd, "_run", self.systemctl)
        self.monkeypatch.setattr(self.installer, "_run", self._host)
        ctx = InstallContext(
            host="host",
            script_dir=self.build_dir.parent.parent,
            build_dir=self.build_dir,
            env={},
            deploy_env={},
            file_map=dict(self.file_map),
            force_update=self.force,
        )
        self.installer.install(ctx)
        return ctx

    def converge(self) -> None:
        self.run()
        self.apt.calls.clear()
        self.systemctl.calls.clear()

    def written(self) -> list[Path]:
        return sorted(p for p in self.sandbox.rglob("*") if p.is_file())


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# --------------------------------------------------------------------------
# converged hosts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "file_specs",
    [specs(), everything(), specs(lxc_guest=True)],
    ids=["bare-metal", "every-exporter", "lxc-guest"],
)
def test_a_converged_redeploy_changes_nothing(harness: Harness, file_specs) -> None:
    harness.use(file_specs)
    harness.converge()
    mtimes = {path: path.stat().st_mtime_ns for path in harness.written()}

    ctx = harness.run()

    assert not ctx.changes.any()
    assert {path: path.stat().st_mtime_ns for path in harness.written()} == mtimes
    assert harness.apt.apt_calls == []
    assert harness.systemctl.actions() == []


def test_first_install_writes_every_file_runs_each_exporter_once_and_verifies(
    harness: Harness,
) -> None:
    harness.use(everything())
    harness.apt.missing = {NODE, SMART, "intel-gpu-tools"}
    harness.systemctl.enabled.clear()
    harness.systemctl.active.clear()

    harness.run()

    for name, (_dest, mode) in harness.file_map.items():
        assert harness.dest(name).read_text(encoding="utf-8") == f"# {name}\n"
        assert oct(harness.dest(name).stat().st_mode & 0o777)[2:] == mode
    actions = harness.systemctl.actions()
    for stem in ("zfs-pool", "hba", "reboot", "disk-label"):
        assert actions.count(f"start {stem}-textfile-exporter.service") == 1
        assert f"enable --now {stem}-textfile-exporter.timer" in actions
    for unit in (
        NODE_UNIT,
        "smartctl_exporter.service",
        "igpu-exporter.service",
        "apcupsd-exporter.service",
    ):
        assert f"enable --now {unit}" in actions
    verified = [c[-1] for c in harness.systemctl.calls if c[1] == "is-active"]
    assert (
        "apcupsd-exporter.service" in verified and "disk-label-textfile-exporter.timer" in verified
    )


# --------------------------------------------------------------------------
# refusals: every one of them before any write
# --------------------------------------------------------------------------


def _refuses(harness: Harness, match: str) -> None:
    before = harness.written()
    with pytest.raises(InstallError, match=match):
        harness.run()
    assert harness.written() == before
    assert harness.systemctl.calls == []
    assert harness.apt.calls == []


def test_no_os_release_refuses(harness: Harness) -> None:
    Path(harness.installer.OS_RELEASE).unlink()
    _refuses(harness, "cannot read")


def test_an_os_release_without_a_codename_refuses(harness: Harness) -> None:
    harness.os_release("ID=debian\n")
    _refuses(harness, "ID/VERSION_CODENAME missing")


def test_a_map_without_the_node_exporter_defaults_refuses(harness: Harness) -> None:
    del harness.file_map["node-exporter.defaults"]
    _refuses(harness, "missing file-map entries: node-exporter.defaults")


@pytest.mark.parametrize("dropped", [group[-1] for group in load_installer().GROUPS])
def test_a_partial_exporter_refuses(harness: Harness, dropped: str) -> None:
    """A unit enabled with nothing behind it, or an ExecStartPre pointing nowhere."""
    harness.use(everything())
    del harness.file_map[dropped]
    _refuses(harness, f"partial exporter in file map: .* without {dropped}")


def test_a_missing_staged_file_refuses(harness: Harness) -> None:
    (harness.build_dir / "reboot-textfile-exporter").unlink()
    _refuses(harness, "missing staged files: reboot-textfile-exporter")


# --------------------------------------------------------------------------
# packages
# --------------------------------------------------------------------------


def test_debian_installs_smartctl_exporter_from_backports(harness: Harness) -> None:
    harness.apt.missing = {SMART}

    harness.run()

    source = Path(harness.installer.BACKPORTS_SOURCE).read_text(encoding="utf-8")
    assert "Suites: trixie-backports\n" in source
    assert harness.apt.apt_calls == [
        ["apt-get", "update", "-qq"],
        ["apt-get", "install", "-y", "-q", "-t", "trixie-backports", SMART],
    ]


def test_a_new_backports_source_alone_does_not_touch_apt(harness: Harness) -> None:
    """The bash refreshed the lists whenever the source changed, even with nothing to install."""
    harness.run()

    assert Path(harness.installer.BACKPORTS_SOURCE).is_file()
    assert harness.apt.apt_calls == []


def test_ubuntu_installs_smartctl_exporter_from_its_own_archive(harness: Harness) -> None:
    harness.os_release(UBUNTU)
    harness.apt.missing = {SMART}

    harness.run()

    assert not Path(harness.installer.BACKPORTS_SOURCE).exists()
    assert ["apt-get", "install", "-y", "-q", SMART] in harness.apt.apt_calls


def test_a_guest_never_installs_smartctl_exporter_or_gpu_tools(harness: Harness) -> None:
    harness.use(specs(lxc_guest=True))
    harness.apt.missing = {NODE, SMART, "intel-gpu-tools"}

    harness.run()

    assert harness.apt.apt_calls[-1] == ["apt-get", "install", "-y", "-q", NODE]
    assert not Path(harness.installer.BACKPORTS_SOURCE).exists()


def test_intel_gpu_tools_come_with_the_gpu_exporter(harness: Harness) -> None:
    harness.use(specs(has_igpu=True))
    harness.apt.missing = {"intel-gpu-tools"}

    harness.run()

    assert ["apt-get", "install", "-y", "-q", "intel-gpu-tools"] in harness.apt.apt_calls


# --------------------------------------------------------------------------
# restarts and oneshots follow their own files
# --------------------------------------------------------------------------


def test_changed_node_exporter_defaults_restart_only_node_exporter(harness: Harness) -> None:
    harness.converge()
    harness.dest("node-exporter.defaults").write_text("drift\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions() == ["daemon-reload", "restart " + NODE_UNIT]


def test_a_changed_textfile_config_runs_its_exporter_without_touching_the_timer(
    harness: Harness,
) -> None:
    harness.use(specs(has_disk_label_overrides=True))
    harness.converge()
    harness.dest("disk-labels.conf").write_text("drift\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions() == ["start disk-label-textfile-exporter.service"]


def test_a_changed_timer_is_reloaded_and_restarted(harness: Harness) -> None:
    harness.converge()
    harness.dest("zfs-pool-textfile-exporter.timer").write_text("drift\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions() == [
        "daemon-reload",
        "daemon-reload",
        "restart zfs-pool-textfile-exporter.timer",
    ]


def test_a_changed_smartctl_override_reloads_and_restarts_the_packaged_unit(
    harness: Harness,
) -> None:
    harness.converge()
    harness.dest("smartctl-exporter-override.conf").write_text("drift\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions() == [
        "daemon-reload",
        "daemon-reload",
        "restart smartctl_exporter.service",
    ]


@pytest.mark.parametrize("entry", ["apcupsd-exporter.py", "apcupsd-exporter.env"])
def test_a_changed_apcupsd_exporter_is_restarted(harness: Harness, entry: str) -> None:
    """The bash only ever ran `enable --now`, so a new script kept the old process."""
    harness.use(specs(has_apcupsd=True))
    harness.converge()
    harness.dest(entry).write_text("drift\n", encoding="utf-8")

    harness.run()

    assert "restart apcupsd-exporter.service" in harness.systemctl.actions()


def test_a_changed_gpu_exporter_unit_is_restarted(harness: Harness) -> None:
    harness.use(specs(has_igpu=True))
    harness.converge()
    harness.dest("igpu-exporter.service").write_text("drift\n", encoding="utf-8")

    harness.run()

    assert harness.systemctl.actions()[-1] == "restart igpu-exporter.service"


def test_force_reruns_every_exporter(harness: Harness) -> None:
    harness.converge()
    harness.force = True

    harness.run()

    actions = harness.systemctl.actions()
    assert "restart " + NODE_UNIT in actions
    assert "restart smartctl_exporter.service" in actions
    assert "start reboot-textfile-exporter.service" in actions


def test_an_enabled_timer_that_is_not_running_is_started(harness: Harness) -> None:
    harness.converge()
    harness.systemctl.active.discard("reboot-textfile-exporter.timer")

    harness.run()

    assert harness.systemctl.actions() == ["start reboot-textfile-exporter.timer"]


def test_a_failing_exporter_fails_the_deploy(harness: Harness) -> None:
    harness.converge()
    harness.dest("reboot-textfile-exporter").write_text("drift\n", encoding="utf-8")
    harness.systemctl.fail_start.add("reboot-textfile-exporter.service")

    with pytest.raises(InstallError, match="reboot-textfile-exporter.service failed"):
        harness.run()


def test_every_unit_that_did_not_come_up_is_named(harness: Harness) -> None:
    harness.use(specs(has_igpu=True))
    harness.converge()
    harness.systemctl.active -= {NODE_UNIT, "igpu-exporter.service"}
    harness.systemctl.dead.update({NODE_UNIT, "igpu-exporter.service"})

    with pytest.raises(
        InstallError, match=f"not active after deploy: {NODE_UNIT}, igpu-exporter.service$"
    ):
        harness.run()


# --------------------------------------------------------------------------
# retiring what the map no longer carries
# --------------------------------------------------------------------------


def test_an_unconfigured_textfile_exporter_is_removed_with_its_stale_prom(
    harness: Harness,
) -> None:
    harness.use(specs(has_hba=True))
    harness.converge()
    prom = harness.seed("/var/lib/prometheus/node-exporter/hba.prom")
    harness.use(specs())

    ctx = harness.run()

    assert not Path(harness.under("/usr/local/bin/hba-textfile-exporter")).exists()
    assert not Path(harness.under("/etc/systemd/system/hba-textfile-exporter.timer")).exists()
    assert not prom.exists()
    actions = harness.systemctl.actions()
    assert actions.index("disable --now hba-textfile-exporter.timer") < actions.index(
        "disable --now hba-textfile-exporter.service"
    )
    assert "daemon-reload" in actions
    assert ctx.changes.any()


def test_a_retired_exporter_is_removed_even_after_its_timer_is_gone(harness: Harness) -> None:
    """The bash keyed the cleanup on the timer, so a half-removed exporter stayed half-removed."""
    script = harness.seed("/usr/local/bin/disk-label-textfile-exporter")
    prom = harness.seed("/var/lib/prometheus/node-exporter/disk-labels.prom")
    harness.use(specs(lxc_guest=True))

    harness.run()

    assert not script.exists()
    assert not prom.exists()


@pytest.mark.parametrize(
    ("flag", "unit", "paths"),
    [
        (
            "has_apcupsd",
            "apcupsd-exporter",
            ("/etc/default/apcupsd-exporter", "/usr/local/bin/apcupsd-exporter"),
        ),
        (
            "has_igpu",
            "igpu-exporter",
            ("/etc/default/igpu-exporter", "/usr/local/bin/igpu-exporter"),
        ),
    ],
)
def test_an_unconfigured_service_exporter_is_stopped_and_removed(
    harness: Harness, flag: str, unit: str, paths: tuple[str, ...]
) -> None:
    harness.use(specs(**{flag: True}))
    harness.converge()
    harness.use(specs())

    harness.run()

    assert f"disable --now {unit}.service" in harness.systemctl.actions()
    for path in (*paths, f"/etc/systemd/system/{unit}.service"):
        assert not Path(harness.under(path)).exists()


def test_unconfigured_disk_labels_and_patch_statuses_are_removed(harness: Harness) -> None:
    harness.use(specs(has_disk_label_overrides=True, has_pve_patch_statuses=True))
    harness.converge()
    prom = harness.seed("/var/lib/prometheus/node-exporter/pve-patches.prom")
    harness.use(specs())

    harness.run()

    assert not Path(harness.under("/etc/homelab/disk-labels.conf")).exists()
    assert not Path(harness.under("/etc/homelab/pve-patch-statuses.conf")).exists()
    assert not prom.exists()


def test_retirement_paths_match_the_orchestrator_destinations(harness: Harness) -> None:
    """The installer hardcodes where retired files live; the orchestrator owns where they go."""
    installer = load_installer()
    destinations = {spec.remote_path for spec in metrics_exporters.FILE_SPECS}
    retired = {
        f"{installer.DEFAULT_DIR}/apcupsd-exporter",
        f"{installer.BIN_DIR}/apcupsd-exporter",
        f"{installer.DEFAULT_DIR}/igpu-exporter",
        f"{installer.BIN_DIR}/igpu-exporter",
        f"{installer.SYSTEMD_DIR}/apcupsd-exporter.service",
        f"{installer.SYSTEMD_DIR}/igpu-exporter.service",
        f"{installer.ETC_HOMELAB}/disk-labels.conf",
        f"{installer.ETC_HOMELAB}/pve-patch-statuses.conf",
    }
    for stem in ("hba-textfile-exporter", "disk-label-textfile-exporter"):
        retired |= {
            f"{installer.BIN_DIR}/{stem}",
            f"{installer.SYSTEMD_DIR}/{stem}.timer",
            f"{installer.SYSTEMD_DIR}/{stem}.service",
        }
    assert retired <= destinations
    assert installer.TEXTFILE_DIR == metrics_exporters.TEXTFILE_DIR
    for script, (stem, configs) in installer.TEXTFILE_EXPORTERS.items():
        assert {script, f"{stem}.service", f"{stem}.timer", *configs} <= {
            spec.build_name for spec in metrics_exporters.FILE_SPECS
        }


# --------------------------------------------------------------------------
# masking units that can never succeed
# --------------------------------------------------------------------------


def test_bare_metal_masks_only_openipmi(harness: Harness) -> None:
    harness.systemctl.installed |= {
        "openipmi.service",
        "nvmf-autoconnect.service",
        "zfs-zed.service",
    }

    harness.run()

    assert harness.systemctl.masked == {"openipmi.service"}


def test_a_container_without_zfs_masks_the_zfs_units(harness: Harness) -> None:
    harness.use(specs(lxc_guest=True))
    harness.container = True
    Path(harness.installer.DEV_ZFS).unlink()
    harness.systemctl.installed |= {
        "openipmi.service",
        "nvmf-autoconnect.service",
        "zfs-mount.service",
        "zfs-share.service",
        "zfs-zed.service",
    }

    harness.run()

    assert harness.systemctl.masked == harness.systemctl.installed


def test_a_container_with_zfs_keeps_the_zfs_units(harness: Harness) -> None:
    """Keyed on the device, so masking can never fire on a host that mounts ZFS."""
    harness.container = True
    harness.systemctl.installed |= {"nvmf-autoconnect.service", "zfs-mount.service"}

    harness.run()

    assert harness.systemctl.masked == {"nvmf-autoconnect.service"}
