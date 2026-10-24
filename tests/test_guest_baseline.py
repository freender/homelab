"""guest-baseline: orchestrator and remote installer (freender/homelab-ops#162).

The installer runs against a fake guest: accounts come from in-memory pwd/grp
tables, host paths live under `tmp_path`, and every command is faked. Pinned:

* **A converged guest is not touched** -- no useradd, no sudoers or drop-in
  rewrite, no sshd reload, no timedatectl/locale-gen/update-locale call.
* **Refusals come before writes**, including a uid or gid another account holds.
* **An existing account is never reshaped**; only missing `sudo` membership is added.
* **A dry run writes nothing** and reports each change it would make.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from homelab.hosts import HostRegistry
from homelab.modules import guest_baseline
from homelab_install import files, sshd, sudoers
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "guest-baseline" / "scripts" / "install.py"
CONFIGS = ROOT / "guest-baseline" / "configs"
TZ = "America/New_York"
EFFECTIVE = (
    "permitrootlogin without-password\n"
    "passwordauthentication no\n"
    "kbdinteractiveauthentication no\n"
    "pubkeyauthentication yes\n"
    "x11forwarding no\n"
    "maxauthtries 3\n"
)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def registry_with(tmp_path: Path, hosts: dict) -> HostRegistry:
    path = tmp_path / "hosts.conf"
    path.write_text(yaml.safe_dump(hosts, sort_keys=False), encoding="utf-8")
    return HostRegistry(path)


def guest(features: dict, host_type: str = "ubuntu") -> dict:
    config = {"type": host_type, "hostname": "g", "user": "root", "sshkey": "infra"}
    return {"config": config, "features": {"guest-baseline": True, **features}}


def test_validate_accepts_the_real_bundle_and_ubuntu_guests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = registry_with(tmp_path, {"g": guest({})})
    monkeypatch.setattr(guest_baseline, "default_registry", lambda _root: registry)

    guest_baseline.validate(ROOT, ["g"])


@pytest.mark.parametrize(
    ("block", "message"),
    [
        (guest({}, host_type="haos"), "^g: guest-baseline only supports ubuntu hosts$"),
        (guest({"ubuntu-setup": {}}), "^g: guest-baseline and ubuntu-setup both own sshd"),
    ],
)
def test_validate_refuses_hosts_it_cannot_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: dict, message: str
) -> None:
    registry = registry_with(tmp_path, {"g": block})
    monkeypatch.setattr(guest_baseline, "default_registry", lambda _root: registry)

    with pytest.raises(ValueError, match=message):
        guest_baseline.validate(ROOT, ["g"])


@pytest.mark.parametrize("missing", ["scripts/install.py", *guest_baseline.STATIC_FILES])
def test_validate_refuses_an_incomplete_module(tmp_path: Path, missing: str) -> None:
    module = tmp_path / "guest-baseline"
    for name in ("scripts/install.py", *(f"configs/{n}" for n in guest_baseline.STATIC_FILES)):
        (module / name).parent.mkdir(parents=True, exist_ok=True)
        (module / name).write_text("x", encoding="utf-8")
    target = module / missing if missing.startswith("scripts") else module / "configs" / missing
    target.unlink()

    with pytest.raises(ValueError, match=f"^missing required file: {target}$"):
        guest_baseline.validate(tmp_path, [])


def test_build_renders_the_bundle(tmp_path: Path) -> None:
    shutil.copytree(CONFIGS, tmp_path / "guest-baseline" / "configs")
    build = tmp_path / "guest-baseline" / "build" / "g"

    assert guest_baseline.build_host(tmp_path, "g") == build

    for name in guest_baseline.STATIC_FILES:
        assert (build / name).read_bytes() == (CONFIGS / name).read_bytes()
    assert (build / "file-map.conf").read_text() == (
        "sudoers|/etc/sudoers.d/freender|440\n"
        "99-homelab-key-only.conf|/etc/ssh/sshd_config.d/99-homelab-key-only.conf|644\n"
        "99-homelab-no-dns-cache.conf|"
        "/etc/systemd/resolved.conf.d/99-homelab-no-dns-cache.conf|644\n"
    )
    assert (build / "env").read_text().splitlines() == [
        "USER_NAME=freender",
        "USER_ID=1000",
        "TIMEZONE=America/New_York",
        "LOCALE=en_US.UTF-8",
        "RETIRED_SSHD_DROP_INS=/etc/ssh/sshd_config.d/99-disable-password-auth.conf",
    ]


class Staged:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, root, connection, remote_root, uploads, installer, *args, **kwargs):
        self.calls.append(
            {
                "root": root,
                "connection": connection,
                "remote_root": remote_root,
                "uploads": uploads,
                "installer": installer,
                "args": args,
            }
            | kwargs
        )


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Staged:
    monkeypatch.setattr(guest_baseline, "build_host", lambda root, host: root / "b" / host)
    monkeypatch.setattr(
        guest_baseline, "connection_for_host", lambda root, host: ("conn", root, host)
    )
    stage = Staged()
    monkeypatch.setattr(guest_baseline, "stage_and_run_remote_installer", stage)
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    return stage


@pytest.mark.parametrize(("dry_run", "plan_only"), [(True, "true"), (False, "false")])
def test_deploy_stages_the_installer_with_plan_only_on_a_dry_run(
    tmp_path: Path, staged: Staged, dry_run: bool, plan_only: str
) -> None:
    guest_baseline.deploy_host(tmp_path, "g", dry_run=dry_run, force=False)

    (call,) = staged.calls
    assert call == {
        "root": tmp_path,
        "connection": ("conn", tmp_path, "g"),
        "remote_root": "/tmp/homelab-guest-baseline",
        "uploads": [
            (tmp_path / "b" / "g", "/tmp/homelab-guest-baseline/build/g"),
            (tmp_path / "guest-baseline" / "scripts", "/tmp/homelab-guest-baseline/scripts"),
        ],
        "installer": "scripts/install.py",
        "args": ("g",),
        "env": {"FORCE_UPDATE": "false", "PLAN_ONLY": plan_only},
        "require_root": True,
        "interpreter": "python3",
        "remote_subdirs": ("build", "src"),
    }


def test_force_reaches_the_installer(tmp_path: Path, staged: Staged) -> None:
    guest_baseline.deploy_host(tmp_path, "g", dry_run=False, force=True)

    assert staged.calls[0]["env"]["FORCE_UPDATE"] == "true"


def test_an_offline_dry_run_never_stages(
    tmp_path: Path, staged: Staged, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    guest_baseline.deploy_host(tmp_path, "g", dry_run=True, force=False)

    assert staged.calls == []


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


def load_installer():
    spec = importlib.util.spec_from_file_location("guest_baseline_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.answers: dict[tuple[str, ...], tuple[int, str]] = {}
        self.effects: dict[tuple[str, ...], object] = {}

    def answer(self, *argv: str, code: int = 0, stdout: str = "") -> None:
        self.answers[argv] = (code, stdout)

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        for length in range(len(argv), 0, -1):
            if tuple(argv[:length]) in self.answers:
                code, stdout = self.answers[tuple(argv[:length])]
                break
        else:
            code, stdout = 0, ""
        effect = self.effects.get(tuple(argv[:1]))
        if effect and not code:
            effect(argv)
        return subprocess.CompletedProcess(argv, code, stdout=stdout)

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)


class Guest:
    """A converged guest: freender exists in sudo, every file already matches."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.installer = load_installer()
        self.root = tmp_path / "host"
        self.build = tmp_path / "stage" / "build" / "g"
        self.build.mkdir(parents=True)
        for name in guest_baseline.STATIC_FILES:
            (self.build / name).write_bytes((CONFIGS / name).read_bytes())

        etc = self.root / "etc"
        self.sudoers = etc / "sudoers.d" / "freender"
        self.drop_in = etc / "ssh/sshd_config.d/99-homelab-key-only.conf"
        self.retired = etc / "ssh/sshd_config.d/99-disable-password-auth.conf"
        self.resolved_drop_in = etc / "systemd/resolved.conf.d/99-homelab-no-dns-cache.conf"
        self.locale_gen = etc / "locale.gen"
        self.default_locale = etc / "default/locale"
        self.home = self.root / "home" / "freender"
        for dest, name, mode in (
            (self.sudoers, "sudoers", 0o440),
            (self.drop_in, "99-homelab-key-only.conf", 0o644),
            (self.resolved_drop_in, "99-homelab-no-dns-cache.conf", 0o644),
        ):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((CONFIGS / name).read_bytes())
            dest.chmod(mode)
        self.locale_gen.write_text("# en_GB.UTF-8 UTF-8\nen_US.UTF-8 UTF-8\n", encoding="utf-8")
        self.default_locale.parent.mkdir(parents=True)
        self.default_locale.write_text("LANG=en_US.UTF-8\n", encoding="utf-8")
        zone = self.root / "usr/share/zoneinfo" / TZ
        zone.parent.mkdir(parents=True)
        zone.write_text("TZif\n", encoding="utf-8")

        for name, path in (
            ("ZONEINFO", self.root / "usr/share/zoneinfo"),
            ("LOCALE_GEN", self.locale_gen),
            ("DEFAULT_LOCALE", self.default_locale),
        ):
            monkeypatch.setattr(self.installer, name, str(path))

        self.run = FakeRun()
        for target in (self.installer, files, sshd, sudoers):
            monkeypatch.setattr(target, "_run", self.run)
        self.run.answer("sshd", "-T", stdout=EFFECTIVE)
        self.run.answer("systemctl", "is-active", "--quiet", "ssh.socket", code=3)
        self.run.answer("timedatectl", "show", stdout=f"{TZ}\n")
        self.run.answer("locale", "-a", stdout="C\nC.utf8\nen_US.utf8\nPOSIX\n")
        self.run.answer("systemctl", "is-active", "--quiet", "systemd-resolved")
        self.resolved_config("[Resolve]\nCache=no\n")
        self.run.effects[("useradd",)] = self._useradd
        self.run.effects[("groupadd",)] = self._groupadd
        self.run.effects[("usermod",)] = self._usermod
        self.binaries = {"locale-gen"}
        monkeypatch.setattr(
            self.installer, "_which", lambda name: f"/x/{name}" if name in self.binaries else None
        )
        self.chowned: list[tuple[Path, int, int]] = []
        monkeypatch.setattr(
            self.installer, "_chown", lambda path, uid, gid: self.chowned.append((path, uid, gid))
        )

        self.users = {
            "root": self._passwd("root", 0, 0, "/root"),
            "freender": self._passwd("freender", 1000, 1000, str(self.home)),
        }
        self.groups = {
            "root": SimpleNamespace(gr_name="root", gr_gid=0, gr_mem=[]),
            "sudo": SimpleNamespace(gr_name="sudo", gr_gid=27, gr_mem=["freender"]),
            "freender": SimpleNamespace(gr_name="freender", gr_gid=1000, gr_mem=[]),
        }
        monkeypatch.setattr(
            self.installer,
            "pwd",
            SimpleNamespace(getpwnam=self._getpwnam, getpwuid=self._getpwuid),
        )
        monkeypatch.setattr(
            self.installer,
            "grp",
            SimpleNamespace(getgrnam=self._getgrnam, getgrgid=self._getgrgid),
        )

        self.env = {
            "USER_NAME": "freender",
            "USER_ID": "1000",
            "TIMEZONE": TZ,
            "LOCALE": "en_US.UTF-8",
            "RETIRED_SSHD_DROP_INS": str(self.retired),
        }
        self.file_map = {
            "sudoers": (str(self.sudoers), "440"),
            "99-homelab-key-only.conf": (str(self.drop_in), "644"),
            "99-homelab-no-dns-cache.conf": (str(self.resolved_drop_in), "644"),
        }
        self.deploy_env: dict[str, str] = {}

    def resolved_config(self, merged: str) -> None:
        """What `systemd-analyze cat-config systemd/resolved.conf` prints."""
        self.run.answer("systemd-analyze", "cat-config", stdout=merged)

    @staticmethod
    def _passwd(name: str, uid: int, gid: int, home: str) -> SimpleNamespace:
        return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid, pw_dir=home)

    def _getpwnam(self, name: str):
        return self.users[name]

    def _getpwuid(self, uid: int):
        for user in self.users.values():
            if user.pw_uid == uid:
                return user
        raise KeyError(uid)

    def _getgrnam(self, name: str):
        return self.groups[name]

    def _getgrgid(self, gid: int):
        for group in self.groups.values():
            if group.gr_gid == gid:
                return group
        raise KeyError(gid)

    def _groupadd(self, argv: list[str]) -> None:
        self.groups[argv[-1]] = SimpleNamespace(gr_name=argv[-1], gr_gid=int(argv[2]), gr_mem=[])

    def _useradd(self, argv: list[str]) -> None:
        name = argv[-1]
        self.users[name] = self._passwd(name, int(argv[3]), int(argv[5]), str(self.home))
        self.groups["sudo"].gr_mem.append(name)

    def _usermod(self, argv: list[str]) -> None:
        self.groups[argv[2]].gr_mem.append(argv[3])

    def remove_user(self) -> None:
        del self.users["freender"]
        del self.groups["freender"]
        self.groups["sudo"].gr_mem.remove("freender")

    def install(self, plan_only: bool = False) -> None:
        ctx = InstallContext(
            host="g",
            script_dir=self.build.parent.parent,
            build_dir=self.build,
            env=dict(self.env),
            deploy_env=self.deploy_env | {"PLAN_ONLY": "true" if plan_only else "false"},
            file_map=dict(self.file_map),
            force_update=False,
        )
        self.installer.install(ctx)

    def snapshot(self) -> dict[str, tuple[int, int, bytes]]:
        return {
            str(path): (path.lstat().st_mtime_ns, path.lstat().st_mode, path.read_bytes())
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Guest:
    return Guest(tmp_path, monkeypatch)


MUTATING = (
    ("useradd",),
    ("groupadd",),
    ("usermod",),
    ("sshd", "-t"),
    ("systemctl", "reload"),
    ("systemctl", "restart"),
    ("timedatectl", "set-timezone"),
    ("locale-gen",),
    ("update-locale",),
)


def assert_no_mutation(host: Guest) -> None:
    for argv in MUTATING:
        assert not host.run.ran(*argv), argv


def test_a_converged_guest_is_not_touched(host: Guest) -> None:
    before = host.snapshot()

    host.install()

    assert host.snapshot() == before
    assert_no_mutation(host)
    assert host.run.ran("sshd", "-T")
    assert host.chowned == []


def test_the_real_drop_in_keeps_key_login_for_root_and_freender() -> None:
    """`./deploy` logs in as root by key; a drop-in that closed that would lock
    every later deploy out, with only the PVE console left."""
    settings = sshd.expected_settings(CONFIGS / "99-homelab-key-only.conf")

    assert settings == {
        "permitrootlogin": "without-password",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "pubkeyauthentication": "yes",
        "x11forwarding": "no",
        "maxauthtries": "3",
    }


def test_the_real_sudoers_asks_for_a_password() -> None:
    text = (CONFIGS / "sudoers").read_text(encoding="utf-8")

    assert "NOPASSWD" not in text
    assert "freender ALL=(ALL:ALL) ALL\n" in text


def test_the_installer_and_orchestrator_agree_on_file_names() -> None:
    installer = load_installer()
    assert {
        installer.SUDOERS,
        installer.SSHD_DROP_IN,
        installer.RESOLVED_DROP_IN,
        installer.AUTHORIZED_KEYS,
    } == set(guest_baseline.STATIC_FILES)
    assert installer.SSHD_DROP_IN == guest_baseline.SSHD_DROP_IN
    assert installer.RESOLVED_DROP_IN == guest_baseline.RESOLVED_DROP_IN


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("key", ["USER_NAME", "USER_ID", "TIMEZONE", "LOCALE"])
def test_a_truncated_env_file_is_refused(host: Guest, key: str) -> None:
    del host.env[key]
    host.remove_user()
    before = host.snapshot()

    with pytest.raises(InstallError, match=key):
        host.install()

    assert host.snapshot() == before
    assert_no_mutation(host)


def test_a_missing_retired_list_is_refused_but_an_empty_one_is_allowed(host: Guest) -> None:
    host.env["RETIRED_SSHD_DROP_INS"] = ""
    host.install()

    del host.env["RETIRED_SSHD_DROP_INS"]
    with pytest.raises(InstallError, match=r"\(missing: RETIRED_SSHD_DROP_INS\)$"):
        host.install()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda h: h.env.update(USER_ID="10x0"), "^USER_ID must be a number, got '10x0'$"),
        (lambda h: h.env.update(TIMEZONE="Mars/Base"), "^timezone data not found for Mars/Base$"),
        (lambda h: h.binaries.clear(), "^locale-gen not found; install the locales package"),
        (
            lambda h: (h.build / "sudoers").unlink(),
            r"^staged bundle is incomplete \(missing: sudoers\)$",
        ),
        (
            lambda h: (h.build / "authorized_keys").unlink(),
            r"^staged bundle is incomplete \(missing: authorized_keys\)$",
        ),
        (
            lambda h: h.file_map.pop("99-homelab-key-only.conf"),
            r"^staged bundle is incomplete \(missing: 99-homelab-key-only.conf\)$",
        ),
        (
            lambda h: h.file_map.pop("99-homelab-no-dns-cache.conf"),
            r"^staged bundle is incomplete \(missing: 99-homelab-no-dns-cache.conf\)$",
        ),
        (
            lambda h: h.run.answer("visudo", code=1),
            "^staged sudoers file is invalid; refusing to install$",
        ),
    ],
)
def test_bad_input_is_refused_before_anything_is_written(host: Guest, change, message) -> None:
    host.remove_user()
    host.sudoers.chmod(0o644)
    host.sudoers.write_text("old\n", encoding="utf-8")
    host.default_locale.write_text("LANG=C\n", encoding="utf-8")
    change(host)
    before = host.snapshot()

    with pytest.raises(InstallError, match=message):
        host.install()

    assert host.snapshot() == before
    assert_no_mutation(host)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda h: h.users.update(other=h._passwd("other", 1000, 1000, "/home/other")),
            "^cannot create freender: uid 1000 belongs to other$",
        ),
        (
            lambda h: h.groups.update(
                other=SimpleNamespace(gr_name="other", gr_gid=1000, gr_mem=[])
            ),
            "^cannot create freender: gid 1000 belongs to group other$",
        ),
        (
            lambda h: h.groups.update(
                freender=SimpleNamespace(gr_name="freender", gr_gid=1001, gr_mem=[])
            ),
            "^cannot create freender: group freender already exists with gid 1001$",
        ),
    ],
)
def test_a_taken_uid_or_gid_is_refused(host: Guest, change, message: str) -> None:
    host.remove_user()
    change(host)

    with pytest.raises(InstallError, match=message):
        host.install()

    assert_no_mutation(host)


def test_an_existing_account_is_not_checked_against_the_uid(host: Guest) -> None:
    host.users["freender"] = host._passwd("freender", 1001, 1001, str(host.home))

    host.install()

    assert_no_mutation(host)


# --- user -------------------------------------------------------------------


def test_a_missing_user_is_created_with_the_homelab_key(host: Guest) -> None:
    host.remove_user()

    host.install()

    assert ["groupadd", "-g", "1000", "freender"] in host.run.calls
    assert [
        "useradd", "-m", "-u", "1000", "-g", "1000", "-G", "sudo", "-s", "/bin/bash", "freender",
    ] in host.run.calls
    keys = host.home / ".ssh" / "authorized_keys"
    assert keys.read_bytes() == (CONFIGS / "authorized_keys").read_bytes()
    assert keys.stat().st_mode & 0o777 == 0o600
    assert (host.home / ".ssh").stat().st_mode & 0o777 == 0o700
    assert host.chowned == [(host.home / ".ssh", 1000, 1000), (keys, 1000, 1000)]


def test_an_existing_group_of_the_right_gid_is_reused(host: Guest) -> None:
    host.remove_user()
    host.groups["freender"] = SimpleNamespace(gr_name="freender", gr_gid=1000, gr_mem=[])

    host.install()

    assert not host.run.ran("groupadd")
    assert host.run.ran("useradd")


def test_an_existing_user_keeps_its_own_keys(host: Guest) -> None:
    keys = host.home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True)
    keys.write_text("ssh-rsa other\n", encoding="utf-8")

    host.install()

    assert keys.read_text(encoding="utf-8") == "ssh-rsa other\n"


def test_an_existing_user_outside_sudo_is_added(host: Guest) -> None:
    host.groups["sudo"].gr_mem.remove("freender")

    host.install()

    assert ["usermod", "-aG", "sudo", "freender"] in host.run.calls
    assert not host.run.ran("useradd")


def test_a_user_whose_primary_group_is_sudo_counts_as_in_sudo(host: Guest) -> None:
    host.groups["sudo"].gr_mem.remove("freender")
    host.users["freender"] = host._passwd("freender", 1000, 27, str(host.home))

    host.install()

    assert not host.run.ran("usermod")


def test_a_failing_useradd_fails_the_deploy(host: Guest) -> None:
    host.remove_user()
    host.run.answer("useradd", code=9)

    with pytest.raises(InstallError, match=r"^useradd .* failed \(exit 9\)$"):
        host.install()


# --- sudoers and sshd -------------------------------------------------------


def test_drifted_sudoers_is_restored_at_440(host: Guest) -> None:
    host.sudoers.chmod(0o644)
    host.sudoers.write_text("freender ALL=(ALL) ALL\n", encoding="utf-8")

    host.install()

    assert host.sudoers.read_bytes() == (CONFIGS / "sudoers").read_bytes()
    assert host.sudoers.stat().st_mode & 0o777 == 0o440


def test_the_retired_drop_in_is_removed_after_the_new_one_passes(host: Guest) -> None:
    host.drop_in.unlink()
    host.retired.write_text("PasswordAuthentication no\n", encoding="utf-8")

    host.install()

    assert host.drop_in.read_bytes() == (CONFIGS / "99-homelab-key-only.conf").read_bytes()
    assert not host.retired.exists()
    assert ["systemctl", "reload", "ssh"] in host.run.calls


def test_an_overriding_earlier_drop_in_fails_the_deploy(host: Guest) -> None:
    host.run.answer("sshd", "-T", stdout=EFFECTIVE.replace("x11forwarding no", "x11forwarding yes"))

    with pytest.raises(InstallError, match="x11forwarding is yes, expected no"):
        host.install()


# --- DNS cache --------------------------------------------------------------


def test_a_missing_dns_cache_drop_in_is_installed_and_resolved_restarted(host: Guest) -> None:
    host.resolved_drop_in.unlink()

    host.install()

    assert host.resolved_drop_in.read_bytes() == (
        CONFIGS / "99-homelab-no-dns-cache.conf"
    ).read_bytes()
    assert ["systemctl", "restart", "systemd-resolved"] in host.run.calls


def test_a_guest_without_resolved_gets_no_drop_in(host: Guest) -> None:
    """The LXCs have no systemd-resolved; a drop-in there would be config for nothing."""
    host.resolved_drop_in.unlink()
    host.run.answer("systemctl", "is-active", "--quiet", "systemd-resolved", code=3)

    host.install()

    assert not host.resolved_drop_in.exists()
    assert not host.run.ran("systemd-analyze")
    assert_no_mutation(host)


def test_a_later_drop_in_turning_the_cache_back_on_fails_the_deploy(host: Guest) -> None:
    host.resolved_drop_in.unlink()
    host.resolved_config("[Resolve]\nCache=no\n[Resolve]\nCache=yes\n")

    with pytest.raises(
        InstallError, match=f"^systemd-resolved Cache is yes after {host.resolved_drop_in}"
    ):
        host.install()

    assert not host.run.ran("systemctl", "restart")


@pytest.mark.parametrize(
    ("merged", "expected"),
    [
        ("", "yes"),
        ("[Resolve]\n#Cache=no\n", "yes"),
        ("[Resolve]\nCache=no\n", "no"),
        ("[Resolve]\nCache = no-negative\n", "no-negative"),
        ("[Resolve]\nCache=no\n[Resolve]\nCache=yes\n", "yes"),
        ("[Other]\nCache=no\n", "yes"),
        ("[Resolve]\n; Cache=no\nDNS=10.0.40.1\n", "yes"),
    ],
)
def test_effective_cache_is_the_last_cache_in_the_resolve_section(
    host: Guest, merged: str, expected: str
) -> None:
    host.resolved_config(merged)

    assert host.installer.effective_cache() == expected


# --- timezone and locale ----------------------------------------------------


def test_a_different_timezone_is_set(host: Guest) -> None:
    host.run.answer("timedatectl", "show", stdout="Etc/UTC\n")

    host.install()

    assert ["timedatectl", "set-timezone", TZ] in host.run.calls


def test_a_missing_locale_is_enabled_and_generated(host: Guest) -> None:
    host.locale_gen.write_text("# en_GB.UTF-8 UTF-8\n# en_US.UTF-8 UTF-8\n", encoding="utf-8")
    host.run.answer("locale", "-a", stdout="C\nC.utf8\nPOSIX\n")

    host.install()

    assert host.locale_gen.read_text(encoding="utf-8") == (
        "# en_GB.UTF-8 UTF-8\nen_US.UTF-8 UTF-8\n"
    )
    assert ["locale-gen"] in host.run.calls


def test_a_generated_locale_is_not_regenerated_even_if_locale_gen_disagrees(host: Guest) -> None:
    host.locale_gen.write_text("# en_US.UTF-8 UTF-8\n", encoding="utf-8")

    host.install()

    assert not host.run.ran("locale-gen")
    assert host.locale_gen.read_text(encoding="utf-8") == "# en_US.UTF-8 UTF-8\n"


@pytest.mark.parametrize("current", ['LANG="C.UTF-8"\n', "", "LC_ALL=C\n"])
def test_a_different_or_missing_lang_is_set(host: Guest, current: str) -> None:
    host.default_locale.write_text(current, encoding="utf-8")

    host.install()

    assert ["update-locale", "LANG=en_US.UTF-8"] in host.run.calls


def test_a_quoted_matching_lang_is_left_alone(host: Guest) -> None:
    host.default_locale.write_text("LANG='en_US.UTF-8'\n", encoding="utf-8")

    host.install()

    assert not host.run.ran("update-locale")


def test_a_missing_default_locale_file_reads_as_unset(host: Guest) -> None:
    host.default_locale.unlink()

    host.install()

    assert ["update-locale", "LANG=en_US.UTF-8"] in host.run.calls


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("en_US.UTF-8 UTF-8\n", "en_US.UTF-8 UTF-8\n"),
        ("#  en_US.UTF-8 UTF-8  \nx\n", "en_US.UTF-8 UTF-8\nx\n"),
        ("# en_US.UTF-8 UTF-8\n# en_US.UTF-8 UTF-8\n", "en_US.UTF-8 UTF-8\n# en_US.UTF-8 UTF-8\n"),
        ("# en_US.UTF-8 UTF-8X\n", "# en_US.UTF-8 UTF-8X\nen_US.UTF-8 UTF-8\n"),
        ("# de_DE.UTF-8 UTF-8", "# de_DE.UTF-8 UTF-8\nen_US.UTF-8 UTF-8\n"),
        ("", "en_US.UTF-8 UTF-8\n"),
    ],
)
def test_enable_in_locale_gen(text: str, expected: str) -> None:
    assert load_installer().enable_in_locale_gen(text, "en_US.UTF-8") == expected


@pytest.mark.parametrize(
    ("locale", "alias"),
    [("en_US.UTF-8", "en_US.utf8"), ("C.UTF-8", "C.utf8"), ("POSIX", "POSIX")],
)
def test_locale_alias_matches_locale_a(locale: str, alias: str) -> None:
    assert load_installer().locale_alias(locale) == alias


# --- dry run ----------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_reports_every_change(
    host: Guest, capsys: pytest.CaptureFixture[str]
) -> None:
    host.remove_user()
    host.sudoers.chmod(0o640)
    host.drop_in.write_text("PasswordAuthentication no\n", encoding="utf-8")
    host.retired.write_text("PasswordAuthentication no\n", encoding="utf-8")
    host.run.answer("sshd", "-T", stdout=EFFECTIVE.replace("maxauthtries 3", "maxauthtries 6"))
    host.run.answer("timedatectl", "show", stdout="Etc/UTC\n")
    host.run.answer("locale", "-a", stdout="C\n")
    host.default_locale.write_text("LANG=C.UTF-8\n", encoding="utf-8")
    host.resolved_drop_in.unlink()
    host.resolved_config("[Resolve]\n")
    before = host.snapshot()

    host.install(plan_only=True)

    assert host.snapshot() == before
    assert_no_mutation(host)
    out = capsys.readouterr().out
    for line in (
        "Would create freender (uid/gid 1000, sudo, bash) with the homelab key",
        f"Would update {host.sudoers}",
        f"Would update {host.drop_in}, check it with sshd -t and reload ssh",
        f"Would remove {host.retired} (superseded)",
        "Effective sshd settings that would change: maxauthtries is 6, expected 3",
        "Would set timezone Etc/UTC -> America/New_York",
        f"Would enable en_US.UTF-8 in {host.locale_gen} and run locale-gen",
        "Would set LANG C.UTF-8 -> en_US.UTF-8",
        f"Would update {host.resolved_drop_in} and restart systemd-resolved",
        "Effective systemd-resolved Cache would change: yes -> no",
        "PLAN_ONLY: nothing written",
    ):
        assert line in out, line


def test_a_converged_dry_run_reports_no_changes(
    host: Guest, capsys: pytest.CaptureFixture[str]
) -> None:
    host.install(plan_only=True)

    out = capsys.readouterr().out
    assert "Would" not in out
    assert "Effective sshd config already matches the drop-in" in out
    assert f"{host.sudoers} unchanged" in out
    assert "systemd-resolved cache already off" in out


def test_a_dry_run_reports_missing_sudo_membership(
    host: Guest, capsys: pytest.CaptureFixture[str]
) -> None:
    host.groups["sudo"].gr_mem.remove("freender")
    host.default_locale.unlink()

    host.install(plan_only=True)

    out = capsys.readouterr().out
    assert "Would add freender to sudo" in out
    assert "Would set LANG (unset) -> en_US.UTF-8" in out
    assert not host.run.ran("usermod")
