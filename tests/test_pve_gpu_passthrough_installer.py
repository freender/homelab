"""pve-gpu-passthrough: remote installer (freender/homelab-ops#30).

Every destination is rebound under `tmp_path` and `zfs`/`update-initramfs`/
`proxmox-boot-tool` are faked. Nothing here touches a real boot config. The
cmdline render and the orchestrator's token guard live in `test_render_golden.py`.

What carries the risk, and what these pin:

* **Refusals come before writes.** A bad cmdline or a missing root dataset must
  leave the host exactly as it was -- including the removal script, which is
  otherwise installed first.
* **A no-op deploy runs no boot command and writes no backup.** The bash backed
  up the cmdline on every run, so three routine deploys pruned the copy a human
  would want after a bad boot.
* **A failed boot command is retried by `--force`**, because a plain redeploy
  finds the files already in place and skips it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.modules import pve_gpu_passthrough as gpu
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "pve-gpu-passthrough" / "scripts" / "install.py"
CMDLINE = f"{gpu.REQUIRED_ROOT_TOKEN} boot=zfs quiet intel_iommu=on iommu=pt\n"
ETC_MODULES = "# /etc/modules\nloop\n"


def load_installer():
    spec = importlib.util.spec_from_file_location("gpu_passthrough_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.codes: dict[str, int] = {}

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.codes.get(argv[0], 0))

    def ran(self, command: str) -> bool:
        return any(call[0] == command for call in self.calls)


class Host:
    """A fake node: installer module rebound under tmp_path, plus its build dir."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.installer = load_installer()
        self.etc = tmp_path / "etc"
        self.build = tmp_path / "stage" / "build" / "ace"
        self.build.mkdir(parents=True)
        scripts = tmp_path / "stage" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "remove-local.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        (self.build / "cmdline").write_text(CMDLINE, encoding="utf-8")

        paths = {
            "KERNEL_CMDLINE": self.etc / "kernel" / "cmdline",
            "ETC_MODULES": self.etc / "modules",
            "LEGACY_BLACKLIST": self.etc / "modprobe.d" / "blacklist.conf",
            "REMOVAL_SCRIPT_DEST": tmp_path / "root" / "pve-gpu-passthrough-remove.sh",
        }
        for name, path in paths.items():
            monkeypatch.setattr(self.installer, name, str(path))
        self.managed = {
            name: str(self.etc / dest.removeprefix("/etc/"))
            for name, dest in self.installer.MANAGED_FILES.items()
        }
        monkeypatch.setattr(self.installer, "MANAGED_FILES", self.managed)
        self.run = FakeRun()
        monkeypatch.setattr(self.installer, "_run", self.run)

        self.cmdline = paths["KERNEL_CMDLINE"]
        self.modules = paths["ETC_MODULES"]
        self.legacy = paths["LEGACY_BLACKLIST"]
        self.removal = paths["REMOVAL_SCRIPT_DEST"]
        self.cmdline.parent.mkdir(parents=True)
        self.cmdline.write_text(CMDLINE, encoding="utf-8")
        self.modules.write_text(ETC_MODULES, encoding="utf-8")
        self.script_dir = tmp_path / "stage"

    def render(self, name: str, content: str) -> None:
        (self.build / name).write_text(content, encoding="utf-8")

    def place(self, name: str, content: str) -> Path:
        dest = Path(self.managed[name])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return dest

    def install(self, force: bool = False) -> None:
        ctx = InstallContext(
            host="ace",
            script_dir=self.script_dir,
            build_dir=self.build,
            env={},
            deploy_env={},
            file_map={},
            force_update=force,
        )
        self.installer.install(ctx)

    def backups(self) -> list[Path]:
        return sorted(self.etc.rglob("*.bak.*"))


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def test_orchestrator_stages_the_python_installer() -> None:
    assert gpu.INSTALLER == "scripts/install.py"
    assert gpu.INTERPRETER == "python3"
    assert (REPO_ROOT / "pve-gpu-passthrough" / gpu.INSTALLER).is_file()
    assert not (REPO_ROOT / "pve-gpu-passthrough" / "scripts" / "install.sh").exists()


def test_both_ends_agree_on_the_root_token_and_destinations() -> None:
    installer = load_installer()
    assert installer.REQUIRED_ROOT_TOKEN == gpu.REQUIRED_ROOT_TOKEN
    assert installer.MANAGED_FILES == {
        "blacklist.conf": gpu.GPU_BLACKLIST_REMOTE_PATH,
        "vfio.conf": gpu.VFIO_REMOTE_PATH,
        "modules": gpu.VFIO_MODULES_REMOTE_PATH,
    }


def test_the_removal_script_the_installer_copies_is_staged() -> None:
    installer = load_installer()
    assert (REPO_ROOT / "pve-gpu-passthrough" / "scripts" / installer.REMOVAL_SCRIPT).is_file()


def test_validate_requires_the_python_installer(tmp_path: Path) -> None:
    configs = tmp_path / "pve-gpu-passthrough" / "configs"
    configs.mkdir(parents=True)
    for name in ("blacklist.conf", "modules", "vfio.conf.tpl"):
        (configs / name).write_text("", encoding="utf-8")
    (configs / "cmdline").write_text(CMDLINE, encoding="utf-8")

    with pytest.raises(ValueError, match="install.py"):
        gpu.validate(tmp_path)


def test_force_reaches_the_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        gpu, "stage_and_run_remote_installer", lambda *args, **kwargs: seen.update(kwargs)
    )
    gpu.stage_and_install(REPO_ROOT, "ace", Path("/nonexistent"), object(), force=True)

    assert seen["env"] == {"FORCE_UPDATE": "true"}
    assert seen["interpreter"] == "python3"
    assert seen["require_root"] is True


# ---------------------------------------------------------------------------
# Refusals: nothing written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmdline", "match"),
    [
        ("boot=zfs quiet\n", "required token"),
        (f"{gpu.REQUIRED_ROOT_TOKEN}x quiet\n", "required token"),
        (f"{gpu.REQUIRED_ROOT_TOKEN} quiet\nsecond line\n", "exactly one line"),
        ("", "exactly one line"),
    ],
)
def test_an_unsafe_cmdline_is_refused_before_anything_is_written(
    host: Host, cmdline: str, match: str
) -> None:
    host.render("cmdline", cmdline)
    host.render("vfio.conf", "options vfio-pci ids=10de:2208\n")

    with pytest.raises(InstallError, match=match):
        host.install()

    assert host.cmdline.read_text(encoding="utf-8") == CMDLINE
    assert not host.removal.exists()
    assert not Path(host.managed["vfio.conf"]).exists()
    assert host.run.calls == []


def test_a_missing_build_cmdline_is_refused(host: Host) -> None:
    (host.build / "cmdline").unlink()
    with pytest.raises(InstallError, match="missing source file"):
        host.install()


def test_a_missing_root_dataset_is_refused_before_anything_is_written(host: Host) -> None:
    host.render("cmdline", f"{gpu.REQUIRED_ROOT_TOKEN} quiet\n")
    host.run.codes["zfs"] = 1

    with pytest.raises(InstallError, match="rpool/ROOT/pve-1"):
        host.install()

    assert host.run.calls == [["zfs", "list", "-H", "-o", "name", "rpool/ROOT/pve-1"]]
    assert host.cmdline.read_text(encoding="utf-8") == CMDLINE
    assert not host.removal.exists()


def test_a_host_without_systemd_boot_is_refused(host: Host) -> None:
    host.cmdline.unlink()
    with pytest.raises(InstallError, match="systemd-boot required"):
        host.install()
    assert not host.cmdline.exists()
    assert host.run.calls == []


# ---------------------------------------------------------------------------
# Converged host
# ---------------------------------------------------------------------------


def test_a_converged_host_runs_no_boot_command_and_writes_no_backup(host: Host) -> None:
    host.render("vfio.conf", "options vfio-pci ids=10de:2208\n")
    host.render("modules", "vfio\n")
    host.place("vfio.conf", "options vfio-pci ids=10de:2208\n")
    host.place("modules", "vfio\n")
    host.removal.parent.mkdir(parents=True)
    host.removal.write_text("#!/bin/bash\n", encoding="utf-8")

    host.install()

    assert host.run.calls == [["zfs", "list", "-H", "-o", "name", "rpool/ROOT/pve-1"]]
    assert host.backups() == []


def test_the_removal_script_is_installed_executable(host: Host) -> None:
    host.install()
    assert host.removal.read_text(encoding="utf-8") == "#!/bin/bash\n"
    assert host.removal.stat().st_mode & 0o777 == 0o755


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------


def test_a_changed_cmdline_is_backed_up_and_refreshes_boot_only(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    new = f"{gpu.REQUIRED_ROOT_TOKEN} boot=zfs quiet video=efifb:off\n"
    host.render("cmdline", new)

    host.install()

    assert host.cmdline.read_text(encoding="utf-8") == new
    assert [path.read_text(encoding="utf-8") for path in host.backups()] == [CMDLINE]
    assert host.run.ran("proxmox-boot-tool")
    assert not host.run.ran("update-initramfs")
    assert "takes effect on next reboot" in capsys.readouterr().out


def test_a_new_vfio_binding_rebuilds_initramfs_without_touching_boot(host: Host) -> None:
    host.render("vfio.conf", "options vfio-pci ids=10de:2208\n")
    host.render("modules", "vfio\nvfio_pci\n")

    host.install()

    assert Path(host.managed["vfio.conf"]).read_text(encoding="utf-8").endswith("2208\n")
    assert Path(host.managed["modules"]).read_text(encoding="utf-8") == "vfio\nvfio_pci\n"
    assert ["update-initramfs", "-u", "-k", "all"] in host.run.calls
    assert not host.run.ran("proxmox-boot-tool")


@pytest.mark.parametrize("name", ["blacklist.conf", "vfio.conf", "modules"])
def test_a_file_no_longer_rendered_is_removed_without_an_include_dir_backup(
    host: Host, name: str
) -> None:
    dest = host.place(name, "stale\n")

    host.install()

    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []
    assert host.run.ran("update-initramfs")


def test_the_initramfs_step_does_not_short_circuit_later_files(host: Host) -> None:
    """One changed file must not stop the next from being synced."""
    host.place("blacklist.conf", "stale\n")
    host.render("modules", "vfio\n")

    host.install()

    assert not Path(host.managed["blacklist.conf"]).exists()
    assert Path(host.managed["modules"]).is_file()


def test_legacy_gpu_blacklists_are_commented_out(host: Host) -> None:
    host.legacy.parent.mkdir(parents=True)
    host.legacy.write_text(
        "blacklist snd\nblacklist i915\nblacklist nvidiafb\n# blacklist nouveau\n",
        encoding="utf-8",
    )

    host.install()

    assert host.legacy.read_text(encoding="utf-8") == (
        "blacklist snd\n"
        "# blacklist i915  # Migrated by pve-gpu-passthrough\n"
        "# blacklist nvidiafb  # Migrated by pve-gpu-passthrough\n"
        "# blacklist nouveau\n"
    )
    assert host.backups() == []
    assert host.run.ran("update-initramfs")


def test_already_migrated_blacklists_are_left_alone(host: Host) -> None:
    """clovis's real state: the migration ran once, and must not run again."""
    host.legacy.parent.mkdir(parents=True)
    text = "# blacklist i915  # Migrated by pve-gpu-passthrough\n"
    host.legacy.write_text(text, encoding="utf-8")

    host.install()

    assert host.legacy.read_text(encoding="utf-8") == text
    assert not host.run.ran("update-initramfs")


def test_vfio_lines_move_out_of_etc_modules_with_a_backup(host: Host) -> None:
    host.modules.write_text(f"{ETC_MODULES}vfio\nvfio_pci\n", encoding="utf-8")
    host.modules.chmod(0o640)

    host.install()

    assert host.modules.read_text(encoding="utf-8") == ETC_MODULES
    assert host.modules.stat().st_mode & 0o777 == 0o640
    assert [path.name.split(".bak.")[0] for path in host.backups()] == ["modules"]
    assert host.run.ran("update-initramfs")


# ---------------------------------------------------------------------------
# Failure and retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["update-initramfs", "proxmox-boot-tool"])
def test_a_failed_boot_command_fails_the_deploy_and_names_the_retry(
    host: Host, command: str
) -> None:
    host.render("cmdline", f"{gpu.REQUIRED_ROOT_TOKEN} quiet\n")
    host.render("modules", "vfio\n")
    host.run.codes[command] = 1

    with pytest.raises(InstallError, match="--force"):
        host.install()


def test_force_reruns_both_boot_commands_on_an_unchanged_host(host: Host) -> None:
    host.install(force=True)

    assert host.run.ran("update-initramfs")
    assert host.run.ran("proxmox-boot-tool")
    assert host.cmdline.read_text(encoding="utf-8") == CMDLINE


def test_a_plain_redeploy_after_a_failure_does_not_retry(host: Host) -> None:
    """The trap `--force` exists for: files in place, so nothing re-runs."""
    host.render("modules", "vfio\n")
    host.run.codes["update-initramfs"] = 1
    with pytest.raises(InstallError):
        host.install()

    host.run.calls.clear()
    host.run.codes.clear()
    host.install()

    assert not host.run.ran("update-initramfs")
