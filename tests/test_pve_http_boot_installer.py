"""Remote installer for pve-http-boot (freender/homelab-ops#30).

Every absolute destination is rebound under `tmp_path`, and the three subprocess
surfaces -- `packages._run` (dpkg/apt), `systemd._run` (systemctl), and the
installer's own `_run` (curl, nginx, systemctl for nginx) -- are faked. Nothing here
touches `/etc`, `/srv`, a real unit, or the network.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from homelab_install import packages, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "pve-http-boot" / "scripts" / "install.py"

TOKEN = "homelab-pve-auto-install:s3cr3t"


def load_installer():
    """Fresh load per test: these rebind the module's destination constants."""
    spec = importlib.util.spec_from_file_location("pve_http_boot_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApt:
    """Every package installed unless named in `missing`; installing fixes it."""

    def __init__(self, missing: set[str] | None = None) -> None:
        self.missing = set(missing or ())
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        if command[0] == "dpkg-query":
            if command[-1] in self.missing:
                return subprocess.CompletedProcess(command, 1, stdout="")
            return subprocess.CompletedProcess(command, 0, stdout="install ok installed")
        if command[:2] == ["apt-get", "install"]:
            self.missing -= set(command[4:])
        return subprocess.CompletedProcess(command, 0, stdout="")

    @property
    def apt_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "apt-get"]


class FakeSystemctl:
    def __init__(self, enabled: set[str] | None = None, active: set[str] | None = None) -> None:
        self.enabled = enabled if enabled is not None else set()
        self.active = active if active is not None else set()
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        verb = command[1]
        if verb == "is-enabled":
            return subprocess.CompletedProcess(command, 0 if command[-1] in self.enabled else 1)
        if verb == "is-active":
            return subprocess.CompletedProcess(command, 0 if command[-1] in self.active else 1)
        return subprocess.CompletedProcess(command, 0)

    def actions(self) -> list[str]:
        return [
            " ".join(call[1:])
            for call in self.calls
            if call[1] not in {"is-enabled", "is-active"}
        ]


class FakeHost:
    """The installer's own `_run`: curl, `nginx -t`, and systemctl against nginx and
    the autoupdate service. `codes` maps a command prefix to its exit status."""

    def __init__(
        self,
        nginx_active: bool = True,
        codes: dict[tuple[str, ...], int] | None = None,
        key_bytes: bytes = b"proxmox key",
    ) -> None:
        self.nginx_active = nginx_active
        self.codes = codes or {}
        self.key_bytes = key_bytes
        self.calls: list[list[str]] = []

    def _code(self, command: list[str]) -> int | None:
        for prefix, code in self.codes.items():
            if tuple(command[: len(prefix)]) == prefix:
                return code
        return None

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        code = self._code(command)
        if command[0] == "curl":
            if code:
                # curl -f can leave a partial file behind on a dropped connection.
                Path(command[-1]).write_bytes(b"trunc")
                return subprocess.CompletedProcess(command, code)
            Path(command[-1]).write_bytes(self.key_bytes)
            return subprocess.CompletedProcess(command, 0)
        if command[:3] == ["systemctl", "is-active", "--quiet"]:
            return subprocess.CompletedProcess(command, 0 if self.nginx_active else 3)
        return subprocess.CompletedProcess(command, code or 0)

    def ran(self, *prefix: str) -> bool:
        return any(tuple(call[: len(prefix)]) == prefix for call in self.calls)


UNIT_DIR = "/etc/systemd/system"
FILE_MAP = (
    ("http-boot-mgmt.conf", "/etc/homelab-http-boot/http-boot-mgmt.conf", "600"),
    ("nginx-http-boot.conf", "/etc/nginx/sites-available/http-boot", "644"),
    ("httpboot-autoexec.ipxe", "/srv/httpboot/httpboot/autoexec.ipxe", "644"),
    ("pve-http-boot-enable", "/usr/local/sbin/pve-http-boot-enable", "755"),
    ("pve-http-boot-autoupdate.service", f"{UNIT_DIR}/pve-http-boot-autoupdate.service", "644"),
    ("pve-http-boot-autoupdate.timer", f"{UNIT_DIR}/pve-http-boot-autoupdate.timer", "644"),
)


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.sandbox = tmp_path / "hostfs"
        self.installer = load_installer()
        for name in (
            "PROXMOX_REPO",
            "PROXMOX_KEY",
            "SITE_AVAILABLE",
            "SITE_LINK",
            "DEFAULT_SITE",
            "LOADER_SOURCE",
            "LOADER_DEST",
            "TOKEN_DEST",
            "BOOT_MENU",
        ):
            setattr(self.installer, name, self.under(getattr(self.installer, name)))

        self.build_dir = tmp_path / "remote" / "build" / "arc"
        self.build_dir.mkdir(parents=True)
        for name, _dest, _mode in FILE_MAP:
            (self.build_dir / name).write_text(f"# {name}\n", encoding="utf-8")
        self.file_map = {name: (self.under(dest), mode) for name, dest, mode in FILE_MAP}

        # A converged arc: repo configured, loader packaged, payload built.
        self.write(self.installer.PROXMOX_REPO, "deb existing\n")
        Path(self.installer.PROXMOX_KEY).parent.mkdir(parents=True)
        self.write(self.installer.LOADER_SOURCE, "snponly")
        self.write(self.installer.BOOT_MENU, "#!ipxe stock menu\n")

        self.apt = FakeApt()
        self.systemctl = FakeSystemctl(
            enabled={self.installer.TIMER}, active={self.installer.TIMER}
        )
        self.host = FakeHost()
        self.deploy_env: dict[str, str] = {"HTTP_BOOT_MGMT_IP": "10.0.0.50"}
        self.force = False

    def under(self, dest: str) -> str:
        return str(self.sandbox / dest.lstrip("/"))

    @staticmethod
    def write(path: str, content: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")

    def stage_token(self) -> None:
        (self.build_dir / self.installer.TOKEN_NAME).write_text(TOKEN, encoding="utf-8")

    def run(self) -> InstallContext:
        self.monkeypatch.setattr(packages, "_run", self.apt)
        self.monkeypatch.setattr(packages, "_apt_updated", False)
        self.monkeypatch.setattr(systemd, "_run", self.systemctl)
        self.monkeypatch.setattr("homelab_install.files._run", self.host)
        self.monkeypatch.setattr(self.installer, "_run", self.host)
        ctx = InstallContext(
            host="arc",
            script_dir=self.tmp_path / "remote",
            build_dir=self.build_dir,
            env={},
            deploy_env=dict(self.deploy_env),
            file_map=dict(self.file_map),
            force_update=self.force,
        )
        self.installer.install(ctx)
        return ctx

    def converge(self) -> None:
        """Run once and reset the call logs, leaving a host that matches the build."""
        self.run()
        self.apt.calls.clear()
        self.systemctl.calls.clear()
        self.host.calls.clear()


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# --------------------------------------------------------------------------
# a converged host
# --------------------------------------------------------------------------


def test_a_converged_redeploy_writes_nothing_and_restarts_nothing(harness: Harness) -> None:
    """The bash rewrote the loader and token and restarted nginx on every deploy."""
    harness.stage_token()
    harness.converge()

    ctx = harness.run()

    assert ctx.changes.names() == ()
    assert harness.systemctl.actions() == []
    assert not harness.host.ran("nginx")
    assert not harness.host.ran("systemctl", "restart")
    assert harness.apt.apt_calls == []


def test_every_file_map_entry_lands_at_its_mode(harness: Harness) -> None:
    harness.run()

    for name, (dest, mode) in harness.file_map.items():
        path = Path(dest)
        assert path.read_text(encoding="utf-8") == f"# {name}\n"
        assert oct(path.stat().st_mode & 0o777)[2:] == mode


# --------------------------------------------------------------------------
# loader
# --------------------------------------------------------------------------


def test_the_snp_bound_loader_is_served_under_the_url_unifi_hands_out(harness: Harness) -> None:
    """snponly.efi, published as ipxe.efi: the DHCP option names that file."""
    assert harness.installer.LOADER_SOURCE.endswith("/usr/lib/ipxe/snponly.efi")
    assert harness.installer.LOADER_DEST.endswith("/srv/httpboot/httpboot/ipxe.efi")

    harness.run()

    loader = Path(harness.installer.LOADER_DEST)
    assert loader.read_text(encoding="utf-8") == "snponly"
    assert loader.stat().st_mode & 0o777 == 0o644


def test_a_missing_packaged_loader_fails_the_deploy(harness: Harness) -> None:
    os.unlink(harness.installer.LOADER_SOURCE)

    with pytest.raises(InstallError, match="snponly.efi"):
        harness.run()


def test_the_ipxe_package_is_ensured(harness: Harness) -> None:
    harness.apt.missing = {"ipxe"}

    harness.run()

    assert ["apt-get", "install", "-y", "-q", "ipxe"] in harness.apt.apt_calls


# --------------------------------------------------------------------------
# token
# --------------------------------------------------------------------------


def test_a_staged_token_is_installed_at_600(harness: Harness) -> None:
    harness.stage_token()

    harness.run()

    token = Path(harness.installer.TOKEN_DEST)
    assert token.read_text(encoding="utf-8") == TOKEN
    assert token.stat().st_mode & 0o777 == 0o600


def test_the_token_is_never_printed(harness: Harness, capsys: pytest.CaptureFixture) -> None:
    harness.stage_token()

    harness.run()

    assert "s3cr3t" not in capsys.readouterr().out


def test_an_unstaged_token_keeps_the_one_on_the_host(harness: Harness) -> None:
    harness.write(harness.installer.TOKEN_DEST, "previous")

    harness.run()

    assert Path(harness.installer.TOKEN_DEST).read_text(encoding="utf-8") == "previous"


def test_no_token_anywhere_warns_without_failing(
    harness: Harness, capsys: pytest.CaptureFixture
) -> None:
    harness.run()

    assert "Token not staged and not present" in capsys.readouterr().out


# --------------------------------------------------------------------------
# nginx
# --------------------------------------------------------------------------


def test_a_fresh_host_links_the_site_and_drops_the_package_default(harness: Harness) -> None:
    default = Path(harness.installer.DEFAULT_SITE)
    default.parent.mkdir(parents=True, exist_ok=True)
    default.symlink_to("/etc/nginx/sites-available/default")

    harness.run()

    link = Path(harness.installer.SITE_LINK)
    assert os.readlink(link) == harness.installer.SITE_AVAILABLE
    assert not default.is_symlink()


def test_a_site_link_pointing_elsewhere_is_replaced(harness: Harness) -> None:
    link = Path(harness.installer.SITE_LINK)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to("/somewhere/else")

    harness.run()

    assert os.readlink(link) == harness.installer.SITE_AVAILABLE


def test_a_changed_vhost_restarts_a_running_nginx_after_testing_it(harness: Harness) -> None:
    harness.converge()
    (harness.build_dir / "nginx-http-boot.conf").write_text("listen 8080;\n", encoding="utf-8")

    harness.run()

    nginx_test = harness.host.calls.index(["nginx", "-t"])
    restart = harness.host.calls.index(["systemctl", "restart", "nginx"])
    assert nginx_test < restart


def test_a_stopped_nginx_is_left_stopped(harness: Harness) -> None:
    """pve-http-boot-enable is how nginx starts; a deploy must not override that."""
    harness.host.nginx_active = False

    harness.run()

    assert not harness.host.ran("systemctl", "restart", "nginx")
    assert not harness.host.ran("systemctl", "start", "nginx")


def test_a_rejected_vhost_is_rolled_back_and_nginx_is_not_restarted(harness: Harness) -> None:
    harness.converge()
    vhost = Path(harness.file_map["nginx-http-boot.conf"][0])
    (harness.build_dir / "nginx-http-boot.conf").write_text("broken\n", encoding="utf-8")
    harness.host.codes = {("nginx", "-t"): 1}

    with pytest.raises(InstallError, match="rolled back"):
        harness.run()

    assert vhost.read_text(encoding="utf-8") == "# nginx-http-boot.conf\n"
    assert not harness.host.ran("systemctl", "restart", "nginx")


def test_a_relinked_site_is_tested_before_nginx_restarts(harness: Harness) -> None:
    """The vhost is unchanged, so install_validated never ran nginx -t, but the
    link change still alters what nginx loads."""
    harness.converge()
    os.unlink(harness.installer.SITE_LINK)
    harness.host.codes = {("nginx", "-t"): 1}

    with pytest.raises(InstallError, match="nginx -t failed"):
        harness.run()

    assert not harness.host.ran("systemctl", "restart", "nginx")


def test_a_failed_nginx_restart_fails_the_deploy(harness: Harness) -> None:
    """It was `systemctl restart nginx || true`."""
    harness.host.codes = {("systemctl", "restart", "nginx"): 1}

    with pytest.raises(InstallError, match="restart nginx failed"):
        harness.run()


# --------------------------------------------------------------------------
# Proxmox repo
# --------------------------------------------------------------------------


def test_an_existing_repo_is_left_alone(harness: Harness) -> None:
    harness.run()

    assert not harness.host.ran("curl")
    assert Path(harness.installer.PROXMOX_REPO).read_text(encoding="utf-8") == "deb existing\n"


def test_a_missing_repo_is_added_and_the_lists_refreshed_before_the_assistant_installs(
    harness: Harness,
) -> None:
    """`curl` was installed first in the same run, which already spent the one
    coalesced update -- the case `packages.sources_changed` exists for."""
    os.unlink(harness.installer.PROXMOX_REPO)
    harness.apt.missing = {"curl", "proxmox-auto-install-assistant"}

    harness.run()

    assert Path(harness.installer.PROXMOX_REPO).read_text(encoding="utf-8") == (
        "deb http://download.proxmox.com/debian/pve trixie pve-no-subscription\n"
    )
    assert Path(harness.installer.PROXMOX_KEY).read_bytes() == b"proxmox key"
    assert harness.apt.apt_calls == [
        ["apt-get", "update", "-qq"],
        ["apt-get", "install", "-y", "-q", "curl"],
        ["apt-get", "update", "-qq"],
        ["apt-get", "install", "-y", "-q", "proxmox-auto-install-assistant"],
    ]


def test_a_failed_key_fetch_fails_without_adding_the_repo_or_a_partial_key(
    harness: Harness,
) -> None:
    os.unlink(harness.installer.PROXMOX_REPO)
    harness.host.codes = {("curl",): 22}

    with pytest.raises(InstallError, match="Proxmox release key"):
        harness.run()

    key = Path(harness.installer.PROXMOX_KEY)
    assert not Path(harness.installer.PROXMOX_REPO).exists()
    assert not key.exists()
    assert not key.with_name(key.name + ".partial").exists()


# --------------------------------------------------------------------------
# autoupdate units and payload
# --------------------------------------------------------------------------


def test_a_changed_timer_is_restarted(harness: Harness) -> None:
    harness.converge()
    (harness.build_dir / "pve-http-boot-autoupdate.timer").write_text(
        "OnCalendar=Mon\n", encoding="utf-8"
    )

    harness.run()

    assert "restart pve-http-boot-autoupdate.timer" in harness.systemctl.actions()


def test_a_service_only_change_reloads_without_restarting_the_timer(harness: Harness) -> None:
    harness.converge()
    (harness.build_dir / "pve-http-boot-autoupdate.service").write_text(
        "TimeoutStartSec=2h\n", encoding="utf-8"
    )

    harness.run()

    assert harness.systemctl.actions() == ["daemon-reload"]


def test_the_autoupdate_service_itself_is_never_started_synchronously(harness: Harness) -> None:
    harness.run()

    assert not any(
        call[:2] == ["systemctl", "start"] and "--no-block" not in call
        for call in harness.host.calls + harness.systemctl.calls
    )


@pytest.mark.parametrize("menu", [None, ""])
def test_a_missing_or_empty_payload_queues_a_build_without_waiting(
    harness: Harness, menu: str | None
) -> None:
    """The timer is weekly; waiting for it leaves netboot on a shell for days."""
    if menu is None:
        os.unlink(harness.installer.BOOT_MENU)
    else:
        Path(harness.installer.BOOT_MENU).write_text(menu, encoding="utf-8")

    harness.run()

    assert harness.host.ran(
        "systemctl", "start", "--no-block", "pve-http-boot-autoupdate.service"
    )


def test_a_served_payload_queues_no_build(harness: Harness) -> None:
    harness.run()

    assert not harness.host.ran("systemctl", "start", "--no-block")


def test_the_closing_hint_uses_the_management_ip_it_was_handed(
    harness: Harness, capsys: pytest.CaptureFixture
) -> None:
    harness.run()

    assert "http://10.0.0.50/httpboot/ipxe.efi" in capsys.readouterr().out


# --------------------------------------------------------------------------
# orchestrator contract
# --------------------------------------------------------------------------


def test_the_orchestrator_runs_the_python_installer_with_the_management_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from homelab.modules import pve_http_boot

    root = tmp_path / "root"
    root.mkdir()
    shutil.copy2(ROOT / "hosts.conf", root / "hosts.conf")
    shutil.copytree(ROOT / "secrets", root / "secrets")
    shutil.copytree(
        ROOT / "pve-http-boot", root / "pve-http-boot", ignore=shutil.ignore_patterns("build")
    )

    captured: dict[str, object] = {}

    def fake_stage(_root, _connection, _remote_root, _uploads, installer, host, **kwargs):
        captured.update(installer=installer, host=host, **kwargs)

    monkeypatch.setenv("HOMELAB_OFFLINE", "1")
    monkeypatch.setattr(pve_http_boot, "diff_many", lambda *_args: [])
    monkeypatch.setattr(pve_http_boot, "stage_and_run_remote_installer", fake_stage)

    pve_http_boot.deploy_host(root, "arc", dry_run=False, force=False)

    assert captured["installer"] == "scripts/install.py"
    assert captured["interpreter"] == "python3"
    assert captured["env"] == {"FORCE_UPDATE": "false", "HTTP_BOOT_MGMT_IP": "10.0.0.50"}
