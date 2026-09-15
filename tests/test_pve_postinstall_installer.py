"""Remote installer for pve-postinstall (freender/homelab-ops#30).

Every absolute destination is rebound under `tmp_path`, and every subprocess surface
-- the installer's `_run`/`_which`, `systemd._run` and `files._run` -- goes through one
fake host. Nothing here touches `/etc`, a real unit, a pool, or the network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

from homelab.modules import pve_postinstall
from homelab_install import files, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError
from homelab_install.main import _parse_env_file

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "pve-postinstall" / "scripts" / "install.py"
TZ = "America/New_York"

REBOUND = (
    "SOURCES_DIR",
    "NO_NAG_DEST",
    "BACKUP_DIR",
    "ZONEINFO_DIR",
    "LOCALTIME",
    "TIMEZONE_FILE",
    "STORAGE_CFG",
    "COROSYNC_CONF",
    "INTERFACES",
    "FSTAB",
    "ALIASES",
    "ALIASES_DB",
)


def load_installer():
    """Fresh load per test: these rebind the module's destination constants."""
    spec = importlib.util.spec_from_file_location("pve_postinstall_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass` resolves annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def done(command: list[str], code: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(command, code, stdout=stdout)


class FakeHost:
    """A converged PVE node: every command succeeds unless its prefix is in `failing`."""

    def __init__(self) -> None:
        self.timezone = TZ
        self.pools = {"rpool", "vm-flash"}
        self.storages = {"vm-flash"}
        self.enabled = {
            "zfs-scrub-monthly@rpool.timer",
            "zfs-scrub-monthly@vm-flash.timer",
            "postfix.service",
        }
        self.active = set(self.enabled)
        self.masked = {"openipmi.service"}
        self.unit_files = {"postfix.service", "openipmi.service", "zfs-scrub-monthly@.timer"}
        self.failing: set[str] = set()
        self.clustered = True
        self.certificate = ""
        self.on_ssh = lambda: None
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(command))
        for prefix in self.failing:
            if " ".join(command).startswith(prefix):
                return done(command, 1)
        match command:
            case ["timedatectl", "show", *_]:
                return done(command, stdout=f"{self.timezone}\n")
            case ["timedatectl", "set-timezone", zone]:
                self.timezone = zone
            case ["zpool", "list", "-H", "-o", "name"]:
                return done(command, stdout="\n".join(sorted(self.pools)) + "\n")
            case ["zpool", "list", pool]:
                return done(command, 0 if pool in self.pools else 1)
            case ["zpool", "import", "-f", pool]:
                self.pools.add(pool)
            case ["pvesm", "status", "--storage", storage]:
                return done(command, 0 if storage in self.storages else 1)
            case ["pvesm", "add", "zfspool", storage, *_]:
                self.storages.add(storage)
            case ["pvesm", "status" | "set", *_]:
                pass
            case ["pvecm", "status"]:
                return done(command, 0 if self.clustered else 2)
            case ["systemctl", "is-enabled", "--quiet", unit]:
                return done(command, 0 if unit in self.enabled else 1)
            case ["systemctl", "is-enabled", unit]:
                return done(command, stdout="masked\n" if unit in self.masked else "enabled\n")
            case ["systemctl", "is-active", "--quiet", unit]:
                return done(command, 0 if unit in self.active else 3)
            case ["systemctl", "list-unit-files", unit] | ["systemctl", "cat", unit]:
                return done(command, 0 if unit in self.unit_files else 1)
            case ["systemctl", "enable", "--now", unit]:
                self.enabled.add(unit)
                self.active.add(unit)
            case ["systemctl", "disable", "--now", unit]:
                self.enabled.discard(unit)
                self.active.discard(unit)
            case ["ssh", *_]:
                self.on_ssh()
            case ["hostname", "-I"]:
                return done(command, stdout="10.0.0.20 10.0.60.20\n")
        return done(command)

    def ran(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if call[: len(prefix)] == list(prefix)]

    def mutations(self) -> list[str]:
        """Every call that could change the host, as text."""
        readonly = (
            ("timedatectl", "show"),
            ("zpool", "list"),
            ("pvesm", "status"),
            ("pvecm", "status"),
            ("systemctl", "is-enabled"),
            ("systemctl", "is-active"),
            ("systemctl", "list-unit-files"),
            ("systemctl", "cat"),
        )
        return [" ".join(call) for call in self.calls if tuple(call[:2]) not in readonly]


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.sandbox = tmp_path / "hostfs"
        self.installer = load_installer()
        for name in REBOUND:
            setattr(self.installer, name, self.under(getattr(self.installer, name)))
        self.installer.ENTERPRISE_SOURCES = tuple(
            self.under(path) for path in self.installer.ENTERPRISE_SOURCES
        )

        self.build_dir = tmp_path / "remote" / "build" / "ace"
        self.build_dir.mkdir(parents=True)
        self.file_map: dict[str, tuple[str, str]] = {}
        for spec in pve_postinstall.FILE_SPECS:
            (self.build_dir / spec.build_name).write_text(
                f"# {spec.build_name}\n", encoding="utf-8"
            )
            self.file_map[spec.build_name] = (self.under(spec.remote_path), spec.mode)
        (self.build_dir / "interfaces").write_text(
            "auto lo\niface lo inet loopback\n", encoding="utf-8"
        )

        self.env = {
            "TIMEZONE": TZ,
            "IMPORT_POOLS": "vm-flash",
            "MOUNTS": "",
            "EXPECTED_CLUSTERED": "true",
            "CLUSTER_LINK0": "10.0.0.20",
        }
        self.binaries = {"pveversion", "zpool", "pvesm", "newaliases"}
        self.host = FakeHost()
        self.hostname = "ace"
        self.force = False
        self.converge_host_files()

    def under(self, dest: str) -> str:
        return str(self.sandbox / dest.lstrip("/"))

    @staticmethod
    def write(path: str | Path, content: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")

    def dest(self, name: str) -> Path:
        return Path(self.file_map[name][0])

    def converge_host_files(self) -> None:
        """Lay down what a previous successful run left behind."""
        for name, (dest, _mode) in self.file_map.items():
            self.write(dest, (self.build_dir / name).read_text(encoding="utf-8"))
        zoneinfo = Path(self.installer.ZONEINFO_DIR, TZ)
        self.write(zoneinfo, "TZif")
        Path(self.installer.LOCALTIME).symlink_to(f"{self.installer.ZONEINFO_DIR}/{TZ}")
        self.write(self.installer.TIMEZONE_FILE, f"{TZ}\n")
        self.write(
            self.installer.INTERFACES, (self.build_dir / "interfaces").read_text(encoding="utf-8")
        )
        self.write(self.installer.COROSYNC_CONF, "totem {}\n")
        self.write(self.installer.FSTAB, "proc /proc proc defaults 0 0\n")
        self.write(self.installer.ALIASES, "postmaster: root\n")
        self.write(self.installer.ALIASES_DB, "db")
        aliases_mtime = Path(self.installer.ALIASES).stat().st_mtime
        os.utime(self.installer.ALIASES_DB, (aliases_mtime + 10, aliases_mtime + 10))

    def run(self) -> InstallContext:
        self.monkeypatch.setattr(self.installer, "_run", self.host)
        self.monkeypatch.setattr(systemd, "_run", self.host)
        self.monkeypatch.setattr(files, "_run", self.host)
        self.monkeypatch.setattr(
            self.installer, "_which", lambda name: name if name in self.binaries else None
        )
        self.monkeypatch.setattr(self.installer, "_short_hostname", lambda: self.hostname)
        self.monkeypatch.setattr(self.installer, "_fetch_certificate", self._certificate)
        ctx = InstallContext(
            host="ace",
            script_dir=self.build_dir.parent.parent,
            build_dir=self.build_dir,
            env=dict(self.env),
            deploy_env={},
            file_map=dict(self.file_map),
            force_update=self.force,
        )
        self.installer.install(ctx)
        return ctx

    def _certificate(self, peer: str) -> str:
        if not self.host.certificate:
            raise OSError("connection refused")
        return self.host.certificate

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path): (
                path.read_bytes() if not path.is_symlink() else str(path.readlink()).encode()
            )
            for path in sorted(self.sandbox.rglob("*"))
            if path.is_file() or path.is_symlink()
        }


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# --- converged host --------------------------------------------------------------


def test_converged_host_is_a_no_op(harness: Harness) -> None:
    before = harness.snapshot()

    ctx = harness.run()

    assert harness.snapshot() == before
    assert not ctx.changes.any()
    assert harness.host.mutations() == ["systemctl reset-failed openipmi.service"]


def test_converged_host_does_not_reload_postfix_or_rebuild_aliases(harness: Harness) -> None:
    harness.run()

    assert not harness.host.ran("newaliases")
    assert not harness.host.ran("systemctl", "reload", "postfix")
    assert not harness.host.ran("systemctl", "reload", "ssh")


# --- refusals before any write ---------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda h: h.env.pop("MOUNTS"), "missing: MOUNTS"),
        (lambda h: h.env.pop("TIMEZONE"), "missing: TIMEZONE"),
        (lambda h: h.env.update(EXPECTED_CLUSTERED=""), "empty: EXPECTED_CLUSTERED"),
        (
            lambda h: h.env.update(EXPECTED_CLUSTERED="ture"),
            "EXPECTED_CLUSTERED must be true or false",
        ),
        (lambda h: h.env.update(MOUNTS="media"), "malformed mount 'media'"),
        (lambda h: h.env.update(MOUNTS="media:relative"), "malformed mount"),
        (lambda h: h.env.update(TIMEZONE="Mars/Olympus"), "timezone data not found"),
        (lambda h: h.binaries.discard("pveversion"), "only runs on a PVE host"),
        (lambda h: h.binaries.discard("zpool"), "zpool not found; cannot import vm-flash"),
        (lambda h: (h.build_dir / "pve-test.sources").unlink(), "missing in .*pve-test.sources"),
        (lambda h: h.file_map.pop("no-nag-script"), "missing file-map entries: no-nag-script"),
    ],
)
def test_refusals_come_before_any_write(harness: Harness, change, message: str) -> None:
    for name in harness.file_map:
        harness.dest(name).write_text("drifted\n", encoding="utf-8")
    harness.write(harness.installer.ENTERPRISE_SOURCES[0], "enterprise\n")
    before = harness.snapshot()
    change(harness)

    with pytest.raises(InstallError, match=message):
        harness.run()

    assert harness.snapshot() == before
    assert harness.host.mutations() == []


def test_empty_optional_keys_are_accepted(harness: Harness) -> None:
    harness.env.update(IMPORT_POOLS="", CLUSTER_LINK0="", EXPECTED_CLUSTERED="false")
    harness.binaries.discard("zpool")

    harness.run()

    assert not harness.host.ran("zpool", "import")


# --- apt state -------------------------------------------------------------------


def test_changed_repo_source_backs_up_sources_dir_first(harness: Harness) -> None:
    harness.dest("proxmox.sources").write_text("old\n", encoding="utf-8")

    ctx = harness.run()

    backups = list(Path(harness.installer.BACKUP_DIR).glob("sources.list.d.*"))
    assert len(backups) == 1
    assert (backups[0] / "proxmox.sources").read_text(encoding="utf-8") == "old\n"
    assert harness.dest("proxmox.sources").read_text(encoding="utf-8") == "# proxmox.sources\n"
    assert ctx.changes.touched("proxmox.sources")


def test_unchanged_sources_are_not_backed_up(harness: Harness) -> None:
    harness.run()

    assert not Path(harness.installer.BACKUP_DIR).exists()


def test_enterprise_sources_are_removed(harness: Harness) -> None:
    for path in harness.installer.ENTERPRISE_SOURCES:
        harness.write(path, "enterprise\n")

    harness.run()

    assert not any(Path(path).exists() for path in harness.installer.ENTERPRISE_SOURCES)


def test_changed_nag_file_backs_up_and_reinstalls_toolkit(harness: Harness) -> None:
    harness.write(harness.installer.NO_NAG_DEST, "old hook\n")

    harness.run()

    backups = list(Path(harness.installer.BACKUP_DIR).glob("no-nag-script.*"))
    assert [path.read_text(encoding="utf-8") for path in backups] == ["old hook\n"]
    assert harness.host.ran("apt-get", "install", "--reinstall")


def test_new_nag_script_reinstalls_toolkit_even_when_hook_unchanged(harness: Harness) -> None:
    harness.dest("pve-remove-nag.sh").unlink()

    harness.run()

    assert len(harness.host.ran("apt-get", "install", "--reinstall")) == 1


def test_failed_toolkit_reinstall_only_warns(harness: Harness, capsys) -> None:
    harness.dest("no-nag-script").unlink()
    harness.host.failing.add("apt-get install --reinstall")

    harness.run()

    assert "Widget toolkit reinstall failed" in capsys.readouterr().out


# --- timezone --------------------------------------------------------------------


def test_drifted_timezone_is_converged(harness: Harness) -> None:
    harness.host.timezone = "UTC"
    Path(harness.installer.LOCALTIME).unlink()
    Path(harness.installer.LOCALTIME).symlink_to("/usr/share/zoneinfo/UTC")
    harness.write(harness.installer.TIMEZONE_FILE, "UTC\n")

    harness.run()

    assert harness.host.ran("timedatectl", "set-timezone", TZ)
    assert (
        str(Path(harness.installer.LOCALTIME).readlink())
        == f"{harness.installer.ZONEINFO_DIR}/{TZ}"
    )
    assert Path(harness.installer.TIMEZONE_FILE).read_text(encoding="utf-8") == f"{TZ}\n"


def test_failed_set_timezone_fails_the_deploy(harness: Harness) -> None:
    harness.host.timezone = "UTC"
    harness.host.failing.add("timedatectl set-timezone")

    with pytest.raises(InstallError, match="set-timezone"):
        harness.run()


# --- sshd ------------------------------------------------------------------------


def test_changed_sshd_drop_in_is_validated_and_reloaded(harness: Harness) -> None:
    harness.dest("sshd-hardening.conf").write_text("PasswordAuthentication yes\n", encoding="utf-8")

    harness.run()

    assert harness.host.ran("sshd", "-t")
    assert harness.host.ran("systemctl", "reload", "ssh")


def test_rejected_sshd_drop_in_is_rolled_back_and_not_reloaded(harness: Harness) -> None:
    harness.dest("sshd-hardening.conf").write_text("PasswordAuthentication yes\n", encoding="utf-8")
    harness.host.failing.add("sshd -t")

    with pytest.raises(InstallError, match="rolled back"):
        harness.run()

    assert (
        harness.dest("sshd-hardening.conf").read_text(encoding="utf-8")
        == "PasswordAuthentication yes\n"
    )
    assert not harness.host.ran("systemctl", "reload", "ssh")


def test_failed_sshd_reload_fails_the_deploy(harness: Harness) -> None:
    harness.dest("sshd-hardening.conf").unlink()
    harness.host.failing.add("systemctl reload ssh")

    with pytest.raises(InstallError, match="reload ssh failed"):
        harness.run()


# --- pools, storage, scrub -------------------------------------------------------


def test_missing_pool_is_imported(harness: Harness) -> None:
    harness.env["IMPORT_POOLS"] = "vm-flash vault-hdd"

    harness.run()

    assert harness.host.ran("zpool", "import", "-f", "vault-hdd")
    assert not harness.host.ran("zpool", "import", "-f", "vm-flash")


def test_failed_pool_import_warns_and_carries_on(harness: Harness, capsys) -> None:
    harness.env["IMPORT_POOLS"] = "vault-hdd"
    harness.host.failing.add("zpool import")
    harness.dest("homelab-pve-cluster-rejoin-helper").unlink()

    harness.run()

    assert "Failed to import pool vault-hdd" in capsys.readouterr().out
    assert harness.host.ran("systemctl", "list-unit-files", "postfix.service")


def test_storage_nodes_reads_only_the_named_section(tmp_path: Path) -> None:
    installer = load_installer()
    installer.STORAGE_CFG = str(tmp_path / "storage.cfg")
    Path(installer.STORAGE_CFG).write_text(
        "dir: local\n\tpath /var/lib/vz\n\tnodes nope\n\n"
        "zfspool: vm-flash\n\tpool vm-flash\n\tnodes clovis,bray\n\n"
        "zfspool: vault-hdd\n\tpool vault-hdd\n\n\tnodes stray\n",
        encoding="utf-8",
    )

    assert installer.storage_nodes("vm-flash") == ["clovis", "bray"]
    # A blank line ends the section, as the bash awk's boundary did.
    assert installer.storage_nodes("vault-hdd") == []
    assert installer.storage_nodes("local") == []
    installer.STORAGE_CFG = str(tmp_path / "absent.cfg")
    assert installer.storage_nodes("vm-flash") == []


def test_node_missing_from_restricted_storage_is_appended(harness: Harness) -> None:
    harness.write(harness.installer.STORAGE_CFG, "zfspool: vm-flash\n\tnodes clovis,bray\n")

    ctx = harness.run()

    assert harness.host.ran("pvesm", "set", "vm-flash", "--nodes", "clovis,bray,ace")
    assert ctx.changes.touched("storage:vm-flash")


def test_unrestricted_storage_is_left_alone(harness: Harness) -> None:
    harness.write(harness.installer.STORAGE_CFG, "zfspool: vm-flash\n\tpool vm-flash\n")

    harness.run()

    assert not harness.host.ran("pvesm", "set")


def test_missing_storage_is_created_for_this_node_on_a_cluster(harness: Harness) -> None:
    harness.host.storages.clear()

    harness.run()

    assert harness.host.ran("pvesm", "add", "zfspool", "vm-flash")[0][-2:] == ["--nodes", "ace"]


def test_missing_storage_on_standalone_node_is_unrestricted(harness: Harness) -> None:
    harness.host.storages.clear()
    Path(harness.installer.COROSYNC_CONF).unlink()
    harness.env["EXPECTED_CLUSTERED"] = "false"

    harness.run()

    assert "--nodes" not in harness.host.ran("pvesm", "add", "zfspool", "vm-flash")[0]


def test_failed_pvesm_warns(harness: Harness, capsys) -> None:
    harness.host.storages.clear()
    harness.host.failing.add("pvesm add")

    ctx = harness.run()

    assert "failed to create vm-flash storage" in capsys.readouterr().out
    assert not ctx.changes.touched("storage:vm-flash")


def test_storage_skipped_without_rpool(harness: Harness) -> None:
    harness.host.pools.discard("rpool")
    harness.host.storages.clear()

    harness.run()

    assert not harness.host.ran("pvesm", "status")


def test_scrub_timers_move_to_native_monthly(harness: Harness) -> None:
    harness.host.enabled = {"zfs-scrub.timer", "zfs-scrub-weekly@rpool.timer", "postfix.service"}
    harness.host.active = set(harness.host.enabled)

    harness.run()

    actions = harness.host.mutations()
    assert "systemctl disable --now zfs-scrub.timer" in actions
    assert "systemctl disable --now zfs-scrub-weekly@rpool.timer" in actions
    assert "systemctl enable --now zfs-scrub-monthly@rpool.timer" in actions
    assert "systemctl enable --now zfs-scrub-monthly@vm-flash.timer" in actions


def test_scrub_timers_skipped_without_native_template(harness: Harness) -> None:
    harness.host.unit_files.discard("zfs-scrub-monthly@.timer")

    harness.run()

    assert not harness.host.ran("zpool", "list", "-H")


def test_openipmi_is_masked(harness: Harness) -> None:
    harness.host.masked.clear()

    harness.run()

    assert harness.host.ran("systemctl", "mask", "openipmi.service")


# --- interfaces ------------------------------------------------------------------


def test_changed_interfaces_are_written_backed_up_and_not_applied(harness: Harness, capsys) -> None:
    Path(harness.installer.INTERFACES).write_text("auto lo\niface lo inet dhcp\n", encoding="utf-8")
    Path(harness.installer.INTERFACES).chmod(0o600)

    harness.run()

    out = capsys.readouterr().out
    assert "changed but is NOT live" in out
    assert "        -iface lo inet dhcp" in out
    assert "        +iface lo inet loopback" in out
    assert "---" not in out.split("Pending change:")[1].split("Apply with")[0]
    interfaces = Path(harness.installer.INTERFACES)
    assert interfaces.read_text(encoding="utf-8").endswith("loopback\n")
    assert interfaces.stat().st_mode & 0o777 == 0o600
    assert len(list(interfaces.parent.glob("interfaces.bak.*"))) == 1
    assert not any("ifreload" in call for call in harness.host.mutations())


def test_unrendered_interfaces_are_skipped(harness: Harness) -> None:
    (harness.build_dir / "interfaces").unlink()
    Path(harness.installer.INTERFACES).write_text("hand edited\n", encoding="utf-8")

    harness.run()

    assert Path(harness.installer.INTERFACES).read_text(encoding="utf-8") == "hand edited\n"


def test_new_interfaces_file_warns_without_diff(harness: Harness, capsys) -> None:
    Path(harness.installer.INTERFACES).unlink()

    harness.run()

    out = capsys.readouterr().out
    assert "NOT live" in out
    assert "Pending change:" not in out


# --- mounts ----------------------------------------------------------------------


def test_new_mount_is_appended_and_mounted(harness: Harness) -> None:
    target = harness.under("/mnt/media")
    harness.env["MOUNTS"] = f"media:{target}"

    ctx = harness.run()

    fstab = Path(harness.installer.FSTAB).read_text(encoding="utf-8")
    assert fstab.endswith(f"LABEL=media {target} auto {harness.installer.MOUNT_OPTIONS} 0 2\n")
    assert Path(target).is_dir()
    assert ctx.changes.touched(harness.installer.FSTAB)
    assert harness.host.ran("mount", "-a")


def test_mount_matching_is_by_field_not_substring(harness: Harness) -> None:
    media = harness.under("/mnt/media")
    harness.write(
        harness.installer.FSTAB,
        f"LABEL=media2 {media}2 auto defaults 0 2\n# LABEL=media {media} auto defaults 0 2",
    )
    harness.env["MOUNTS"] = f"media:{media}"

    harness.run()

    lines = Path(harness.installer.FSTAB).read_text(encoding="utf-8").splitlines()
    assert lines[-1].startswith(f"LABEL=media {media} ")
    assert len(lines) == 3


def test_existing_mount_is_not_duplicated(harness: Harness) -> None:
    media = harness.under("/mnt/media")
    harness.write(harness.installer.FSTAB, f"UUID=abc {media} ext4 defaults 0 2\n")
    harness.env["MOUNTS"] = f"media:{media} media:{media}"

    ctx = harness.run()

    assert not ctx.changes.touched(harness.installer.FSTAB)


def test_failed_mount_all_fails_the_deploy(harness: Harness) -> None:
    harness.env["MOUNTS"] = f"media:{harness.under('/mnt/media')}"
    harness.host.failing.add("mount -a")

    with pytest.raises(InstallError, match="mount -a failed"):
        harness.run()


# --- cluster join report ---------------------------------------------------------


def test_clustered_node_reports_membership(harness: Harness, capsys) -> None:
    harness.run()

    assert "Cluster membership detected" in capsys.readouterr().out
    assert not harness.host.ran("ssh")


def test_standalone_node_skips_cluster_report(harness: Harness) -> None:
    harness.env["EXPECTED_CLUSTERED"] = "false"
    Path(harness.installer.COROSYNC_CONF).unlink()

    harness.run()

    assert not harness.host.ran("pvecm")


def test_unclustered_node_cleans_up_from_peer_and_prints_join(harness: Harness, capsys) -> None:
    Path(harness.installer.COROSYNC_CONF).unlink()
    pem = ssl.DER_cert_to_PEM_cert(b"certificate-bytes")
    harness.host.certificate = pem

    harness.run()

    ssh = harness.host.ran("ssh")
    assert len(ssh) == 1
    assert ssh[0][-2] == "root@bray.freender.internal"
    assert ssh[0][-1].endswith("homelab-pve-cluster-rejoin-helper ace bray.freender.internal")
    digest = hashlib.sha256(b"certificate-bytes").hexdigest().upper()
    fingerprint = ":".join(digest[i : i + 2] for i in range(0, 64, 2))
    assert (
        f"pvecm add bray.freender.internal --fingerprint {fingerprint} --link0 10.0.0.20"
        in capsys.readouterr().out
    )
    assert not harness.host.ran("pvecm", "add")


def test_failed_peer_cleanup_tries_each_peer_once_and_skips_itself(
    harness: Harness, capsys
) -> None:
    Path(harness.installer.COROSYNC_CONF).unlink()
    harness.hostname = "bray"
    harness.env["CLUSTER_LINK0"] = ""
    harness.host.failing.add("ssh")

    harness.run()

    peers = [call[-2] for call in harness.host.ran("ssh")]
    assert peers == ["root@ace.freender.internal", "root@clovis.freender.internal"]
    out = capsys.readouterr().out
    assert "homelab-pve-cluster-rejoin-helper bray ace.freender.internal" in out
    assert "fingerprint unavailable" in out
    assert "pvecm add ace.freender.internal --link0 10.0.0.20" in out


def test_cluster_config_appearing_after_cleanup_skips_manual_join(harness: Harness, capsys) -> None:
    corosync = Path(harness.installer.COROSYNC_CONF)
    corosync.unlink()
    harness.host.on_ssh = lambda: corosync.write_text("totem {}\n", encoding="utf-8")
    harness.host.clustered = False

    harness.run()

    assert "manual pvecm add not needed" in capsys.readouterr().out


# --- postfix ---------------------------------------------------------------------


def test_stale_aliases_are_rebuilt(harness: Harness) -> None:
    Path(harness.installer.ALIASES_DB).unlink()

    harness.run()

    assert harness.host.ran("newaliases")


def test_postfix_not_running_is_started(harness: Harness) -> None:
    harness.host.active.discard("postfix.service")

    harness.run()

    assert harness.host.ran("systemctl", "start", "postfix.service")


def test_absent_postfix_is_skipped(harness: Harness) -> None:
    harness.host.unit_files.discard("postfix.service")
    Path(harness.installer.ALIASES_DB).unlink()

    harness.run()

    assert not harness.host.ran("newaliases")


# --- orchestrator wire -----------------------------------------------------------


def test_orchestrator_env_round_trips_into_installer_settings(tmp_path: Path) -> None:
    settings = pve_postinstall.HostSettings(
        host_type="pve",
        timezone=TZ,
        import_pools="vm-flash vault-hdd",
        mounts="media:/mnt/media backup:/mnt/backup",
        expected_clustered="false",
        cluster_link0="",
    )
    pve_postinstall.write_installer_env(tmp_path / "env", settings)
    ctx = InstallContext(
        host="osiris",
        script_dir=tmp_path,
        build_dir=tmp_path,
        env=_parse_env_file(tmp_path / "env"),
        deploy_env={},
        file_map={},
        force_update=False,
    )

    parsed = load_installer().read_settings(ctx)

    assert parsed.timezone == TZ
    assert parsed.import_pools == ("vm-flash", "vault-hdd")
    assert parsed.mounts == (("media", "/mnt/media"), ("backup", "/mnt/backup"))
    assert parsed.expected_clustered is False
    assert parsed.cluster_link0 == ""
