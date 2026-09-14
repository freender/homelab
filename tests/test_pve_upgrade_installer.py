"""Guards for pve-upgrade's remote installer (freender/homelab-ops#30).

Separate from `test_pve_upgrade_gate.py`, which pins the *deploy-time* gate
(`--confirm-upgrade`, exclusion from `deploy all`). This file pins what happens
once the installer is already running on a PVE node, PBS or PDM host.

Two behaviours carry real risk and neither is visible on the host afterwards:

* **Pause must actually stop the upgrade.** `PAUSED` arrives on the process
  environment, and a misread there is silent -- the host takes a live
  dist-upgrade the operator believed was paused.
* **The reboot report must never become a reboot.** These are cluster nodes; the
  installer's job is to report, and the decision is a human one made against a
  named plan.

The kernel comparison is tested against real filenames rather than a mock,
because the thing that can be wrong about it is the version *ordering*, and a
mock would encode whatever ordering the test author assumed.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "pve-upgrade" / "scripts" / "install.py"


def load_installer():
    """Fresh load per test: these rebind module-level paths to a tmp_path."""
    spec = importlib.util.spec_from_file_location("pve_upgrade_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_ctx(tmp_path: Path, deploy_env: dict[str, str] | None = None) -> InstallContext:
    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    return InstallContext(
        host="testhost",
        script_dir=tmp_path,
        build_dir=build_dir,
        env={},
        deploy_env=dict(deploy_env or {}),
        file_map={},
        force_update=False,
    )


# ---------------------------------------------------------------------------
# Kernel version ordering
# ---------------------------------------------------------------------------


def test_newest_kernel_compares_numerically_not_lexically(tmp_path: Path) -> None:
    """`6.14.11-4` is newer than `6.14.9-1`, but sorts *below* it as a string.

    This is the case that matters: a node one point release behind would be
    reported as already running the newest kernel, so the reboot warning that
    should have fired after a kernel upgrade never would.
    """
    installer = load_installer()
    boot = tmp_path / "boot"
    boot.mkdir()
    for version in ("6.14.9-1-pve", "6.14.11-4-pve", "6.14.10-2-pve"):
        (boot / f"vmlinuz-{version}").write_text("", encoding="utf-8")
    installer.BOOT_DIR = boot

    assert installer.newest_installed_kernel() == "6.14.11-4-pve"


def test_newest_kernel_is_none_on_a_host_with_no_kernel_of_its_own(tmp_path: Path) -> None:
    """An LXC boots the host's kernel and has no /boot/vmlinuz-*. That is the
    healthy case, not an error -- there is no such thing as a pending kernel."""
    installer = load_installer()
    boot = tmp_path / "boot"
    boot.mkdir()
    installer.BOOT_DIR = boot

    assert installer.newest_installed_kernel() is None


def test_version_key_orders_a_realistic_pve_kernel_series() -> None:
    installer = load_installer()
    versions = ["6.8.12-4-pve", "6.14.11-4-pve", "6.8.4-2-pve", "6.14.9-1-pve"]

    assert sorted(versions, key=installer._version_key) == [
        "6.8.4-2-pve",
        "6.8.12-4-pve",
        "6.14.9-1-pve",
        "6.14.11-4-pve",
    ]


def test_version_key_never_compares_an_int_against_a_str() -> None:
    """Mixed-type tuples raise TypeError in Python, which would turn a cosmetic
    ordering question into a crashed deploy on a host with an odd kernel name."""
    installer = load_installer()

    assert sorted(["6.8.12-pve", "6-pve", "abc", "6.8"], key=installer._version_key)


# ---------------------------------------------------------------------------
# Reboot reporting
# ---------------------------------------------------------------------------


def test_reboot_reason_reports_the_flag_when_present(tmp_path: Path) -> None:
    installer = load_installer()
    flag = tmp_path / "reboot-required"
    flag.write_text("", encoding="utf-8")
    installer.REBOOT_REQUIRED = flag
    installer.BOOT_DIR = tmp_path / "boot"

    assert installer.reboot_reason("6.14.11-4-pve") == "reboot-required flag"


def test_reboot_reason_falls_back_to_the_kernel_comparison(tmp_path: Path) -> None:
    """PVE and PBS do not ship update-notifier-common, so the flag is never
    written there even after a kernel upgrade. The fallback is what actually
    fires on the hosts this module targets."""
    installer = load_installer()
    installer.REBOOT_REQUIRED = tmp_path / "absent"
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-6.14.11-4-pve").write_text("", encoding="utf-8")
    installer.BOOT_DIR = boot

    assert installer.reboot_reason("6.14.9-1-pve") == (
        "running 6.14.9-1-pve, newest installed 6.14.11-4-pve"
    )


def test_reboot_reason_is_none_when_running_the_newest_kernel(tmp_path: Path) -> None:
    installer = load_installer()
    installer.REBOOT_REQUIRED = tmp_path / "absent"
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-6.14.11-4-pve").write_text("", encoding="utf-8")
    installer.BOOT_DIR = boot

    assert installer.reboot_reason("6.14.11-4-pve") is None


def test_reboot_reason_is_none_on_a_kernel_less_host(tmp_path: Path) -> None:
    installer = load_installer()
    installer.REBOOT_REQUIRED = tmp_path / "absent"
    boot = tmp_path / "boot"
    boot.mkdir()
    installer.BOOT_DIR = boot

    assert installer.reboot_reason("6.14.9-1-pve") is None


# ---------------------------------------------------------------------------
# install()
# ---------------------------------------------------------------------------


def _no_upgrade(monkeypatch: pytest.MonkeyPatch, installer) -> list[list[str]]:
    """Record apt calls; fail loudly if anything else is shelled out."""
    calls: list[list[str]] = []

    def fake(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.packages, "_run", fake)
    return calls


def test_paused_host_is_not_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The expensive failure: a host the operator paused taking a live
    dist-upgrade anyway."""
    installer = load_installer()
    calls = _no_upgrade(monkeypatch, installer)

    installer.install(make_ctx(tmp_path, {"PAUSED": "true"}))

    assert calls == []
    assert "Paused via pve-upgrade.paused" in capsys.readouterr().out


def test_unpaused_host_runs_update_then_dist_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    calls = _no_upgrade(monkeypatch, installer)
    installer.REBOOT_REQUIRED = tmp_path / "absent"
    installer.BOOT_DIR = tmp_path / "noboot"

    installer.install(make_ctx(tmp_path, {"PAUSED": "false"}))

    assert calls == [
        ["apt-get", "update", "-qq"],
        ["apt-get", "-y", "dist-upgrade"],
    ]


def test_a_typo_in_paused_fails_instead_of_upgrading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    calls = _no_upgrade(monkeypatch, installer)

    with pytest.raises(InstallError, match="must be true or false"):
        installer.install(make_ctx(tmp_path, {"PAUSED": "ture"}))

    assert calls == []


def test_missing_paused_defaults_to_running_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator always sends PAUSED, so absent means something upstream
    broke -- but defaulting to *not* paused matches the bash and keeps the
    on-demand escape hatch working when it is needed most."""
    installer = load_installer()
    calls = _no_upgrade(monkeypatch, installer)
    installer.REBOOT_REQUIRED = tmp_path / "absent"
    installer.BOOT_DIR = tmp_path / "noboot"

    installer.install(make_ctx(tmp_path))

    assert ["apt-get", "-y", "dist-upgrade"] in calls


def test_installer_warns_but_never_reboots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The rail that must not regress: this runs on live cluster nodes, and a
    reboot here could take quorum or a running backup with it."""
    installer = load_installer()
    calls = _no_upgrade(monkeypatch, installer)
    flag = tmp_path / "reboot-required"
    flag.write_text("", encoding="utf-8")
    installer.REBOOT_REQUIRED = flag
    installer.BOOT_DIR = tmp_path / "noboot"

    installer.install(make_ctx(tmp_path, {"PAUSED": "false"}))

    output = capsys.readouterr().out
    assert "Reboot required" in output
    assert not any("reboot" in " ".join(call).lower() for call in calls)
    assert not any("shutdown" in " ".join(call).lower() for call in calls)


def test_installer_source_contains_no_reboot_call() -> None:
    """Belt and braces against the above: a reboot could be added on a path the
    behavioural test does not reach, and this is a module where that must fail
    review loudly rather than quietly."""
    source = INSTALLER_PATH.read_text(encoding="utf-8")

    assert "systemctl reboot" not in source
    assert "os.system" not in source
    for forbidden in ('"reboot"', "'reboot'", '"shutdown"', "'shutdown'"):
        assert forbidden not in source
