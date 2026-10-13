"""Guards for base-packages.

Two things are worth pinning. First, the baseline actually reaches every
apt-managed host: this module exists because `mbuffer`/`vim`/`mc` were installed
by `pve-postinstall` and therefore guaranteed on four hosts out of fourteen, and
a host silently dropping out of the feature set would recreate that gap without
any visible failure. Second, `mbuffer` must survive in the baseline, because
`zfs-automation`'s replication jobs pipe through it -- removing it would break
replication on the next rebuild, not at deploy time.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.hosts import default_registry
from homelab.modules import MODULE_ORDER, base_packages
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError
from homelab_install.packages import APT_GET

ROOT = Path(__file__).resolve().parents[1]

HOSTS_HEADER = """\
{host}:
  config:
    type: ubuntu
    hostname: {host}.internal
    user: root
    sshkey: infra
  features:
"""


def write_hosts(tmp_path: Path, body: str) -> Path:
    (tmp_path / "hosts.conf").write_text(body, encoding="utf-8")
    return tmp_path


def test_baseline_includes_mbuffer_for_zfs_replication() -> None:
    # zfs-automation pipes zfs send/recv through mbuffer. It used to come from
    # pve-postinstall; base-packages owns it now and must keep it.
    assert "mbuffer" in base_packages.BASE_PACKAGES
    assert "ripgrep" in base_packages.BASE_PACKAGES


def test_runs_before_zfs_automation() -> None:
    # Ordering is the reason mbuffer is safe to move here: the package has to be
    # installed before the replication units that use it are written.
    assert MODULE_ORDER.index("base-packages") < MODULE_ORDER.index("zfs-automation")
    assert MODULE_ORDER[0] == "base-packages"


def test_enabled_on_every_apt_managed_host() -> None:
    """Every host that apt can reach must carry the feature.

    macOS (exo), the Arch-based Pi-KVM appliance and the Home Assistant OS VM
    (zion) are the only legitimate exclusions; anything else missing is the
    drift this module was written to stop.
    """
    registry = default_registry(ROOT)
    non_apt = {"exo", "pi-kvm", "zion"}
    expected = {host for host in registry.list_hosts() if host not in non_apt}
    actual = set(registry.list_hosts(feature="base-packages"))
    assert actual == expected, f"missing base-packages: {sorted(expected - actual)}"


def test_non_apt_hosts_are_excluded() -> None:
    registry = default_registry(ROOT)
    enabled = set(registry.list_hosts(feature="base-packages"))
    assert "exo" not in enabled
    assert "pi-kvm" not in enabled
    assert "zion" not in enabled


def test_extra_packages_appended_after_baseline(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="tower")
        + "    base-packages:\n      extra:\n        - smartmontools\n",
    )
    packages = base_packages.packages_for_host(root, "tower")
    assert packages[: len(base_packages.BASE_PACKAGES)] == list(base_packages.BASE_PACKAGES)
    assert packages[-1] == "smartmontools"


def test_extra_does_not_duplicate_baseline(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="tower")
        + "    base-packages:\n      extra:\n        - vim\n",
    )
    packages = base_packages.packages_for_host(root, "tower")
    assert packages.count("vim") == 1


def test_bare_feature_gets_the_baseline(tmp_path: Path) -> None:
    root = write_hosts(tmp_path, HOSTS_HEADER.format(host="tower") + "    base-packages:\n")
    assert base_packages.packages_for_host(root, "tower") == list(base_packages.BASE_PACKAGES)


def test_vm_gets_the_vm_baseline_before_extra(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="vm")
        + "    pve-guest:\n      kind: qemu\n"
        + "    base-packages:\n      extra:\n        - rsync\n        - yq\n",
    )
    expected = [*base_packages.BASE_PACKAGES, *base_packages.VM_BASE_PACKAGES, "yq"]
    assert base_packages.packages_for_host(root, "vm") == expected


def test_lxc_and_bare_hosts_skip_the_vm_baseline(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="ct")
        + "    pve-guest:\n      kind: lxc\n    base-packages:\n",
    )
    assert base_packages.packages_for_host(root, "ct") == list(base_packages.BASE_PACKAGES)


def test_vm_baseline_carries_what_other_modules_need() -> None:
    # guest-baseline refuses without locale-gen (#162); replication's fs-freeze
    # on live migration needs the guest agent (#125).
    assert "locales" in base_packages.VM_BASE_PACKAGES
    assert "qemu-guest-agent" in base_packages.VM_BASE_PACKAGES


def test_vm_extra_does_not_repeat_the_vm_baseline() -> None:
    """A package in both lists is dead config that drifts: the VM baseline owns it."""
    registry = default_registry(ROOT)
    for host in registry.list_hosts(feature="base-packages"):
        if registry.get(host, "pve-guest.kind", None) != "qemu":
            continue
        extra = registry.get(host, "base-packages.extra", None) or []
        repeated = set(extra) & set(base_packages.VM_BASE_PACKAGES)
        assert not repeated, f"{host}: drop {sorted(repeated)} from base-packages.extra"


def test_bad_extra_type_is_rejected(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="tower") + "    base-packages:\n      extra: 5\n",
    )
    with pytest.raises(ValueError):
        base_packages.packages_for_host(root, "tower")


# --- the port to homelab_install (freender/homelab-ops#30) -------------------


def test_ships_exactly_one_installer() -> None:
    """The migration rule: `install.py` added and `install.sh` deleted in the
    same commit, never both present. Both present would leave which one runs
    decided by `base_packages.py` alone, with a stale copy of the logic beside
    it."""
    scripts = ROOT / "base-packages" / "scripts"

    assert (scripts / "install.py").is_file()
    assert not (scripts / "install.sh").exists()
    assert sorted(path.name for path in scripts.glob("install*")) == ["install.py"]


def test_declares_the_python_installer_pair() -> None:
    """The two must agree: the `.py` suffix is what makes the staging step upload
    `homelab_install` and set `PYTHONPATH`, so a name reverted to `install.sh` would take
    the library with it silently and the installer would fail to import on the
    target."""
    assert base_packages.INSTALLER == "scripts/install.py"
    assert base_packages.INTERPRETER == "python3"
    assert (ROOT / "base-packages" / base_packages.INSTALLER).is_file()


def test_the_installer_reads_its_package_list_from_the_deploy_env() -> None:
    """`BASE_PACKAGES` is passed via `env=` on the remote command line, because
    `simple_root_installer_deploy` stages no build directory for this module to
    render an env file into. Reading `ctx.env` instead would find `{}` on every
    host.

    Asserted on the parsed source rather than a substring, because the docstring
    names `ctx.env` to explain why it is the wrong one; and on the source rather
    than by importing, because importing would put a second module named
    `install` on `sys.path`.
    """
    source = (ROOT / "base-packages" / "scripts" / "install.py").read_text(encoding="utf-8")

    attributes = {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ctx"
    }

    assert attributes == {"deploy_env"}


# --- backports_kernel ---------------------------------------------------------


def test_backports_kernel_defaults_off(tmp_path: Path) -> None:
    root = write_hosts(tmp_path, HOSTS_HEADER.format(host="vm") + "    base-packages:\n")
    assert base_packages.backports_kernel(root, "vm") is False


def test_backports_kernel_reads_the_flag(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="vm") + "    base-packages:\n      backports_kernel: true\n",
    )
    assert base_packages.backports_kernel(root, "vm") is True


def test_backports_kernel_rejects_a_non_boolean(tmp_path: Path) -> None:
    root = write_hosts(
        tmp_path,
        HOSTS_HEADER.format(host="vm") + "    base-packages:\n      backports_kernel: 7\n",
    )
    with pytest.raises(ValueError, match="backports_kernel"):
        base_packages.backports_kernel(root, "vm")


def test_riven_tracks_the_backports_kernel() -> None:
    # riven is a VM and boots its own kernel; the PVE nodes run Proxmox's.
    assert base_packages.backports_kernel(ROOT, "riven") is True
    assert base_packages.backports_kernel(ROOT, "ace") is False


INSTALLER_PATH = ROOT / "base-packages" / "scripts" / "install.py"
DEBIAN = 'ID=debian\nVERSION_CODENAME=trixie\n'
STABLE = "6.12.111-1"
BACKPORTS = "7.2.6-1~bpo13+1"


def load_installer():
    spec = importlib.util.spec_from_file_location("base_packages_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeKernelApt:
    """dpkg-query reports `version`; `install -t` moves it to `after`."""

    def __init__(self, version: str, after: str = BACKPORTS, code: int = 0) -> None:
        self.version = version
        self.after = after
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        if cmd[0] == "dpkg-query":
            found = bool(self.version)
            return subprocess.CompletedProcess(cmd, 0 if found else 1, self.version, "")
        if "install" in cmd and self.code == 0:
            self.version = self.after
        return subprocess.CompletedProcess(cmd, self.code, "", "")

    def apt_calls(self) -> list[list[str]]:
        prefix = list(APT_GET)
        return [call[len(prefix) :] for call in self.calls if call[: len(prefix)] == prefix]


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    installer = load_installer()
    release = tmp_path / "os-release"
    release.write_text(DEBIAN, encoding="utf-8")
    monkeypatch.setattr(installer, "OS_RELEASE", str(release))

    def use(fake: FakeKernelApt) -> FakeKernelApt:
        monkeypatch.setattr(installer, "_run", fake)
        return fake

    return installer, release, use


def test_moves_a_stable_kernel_to_backports(kernel) -> None:
    installer, _release, use = kernel
    fake = use(FakeKernelApt(STABLE))

    installer.ensure_backports_kernel()

    assert fake.apt_calls() == [
        ["update", "-qq"],
        ["install", "-y", "-q", "-t", "trixie-backports", "linux-image-amd64"],
    ]


def test_a_backports_kernel_is_left_alone(kernel) -> None:
    # The daily dist-upgrade owns upgrades from here; the deploy must not hit apt.
    installer, _release, use = kernel
    fake = use(FakeKernelApt(BACKPORTS))

    installer.ensure_backports_kernel()

    assert fake.apt_calls() == []


def test_installs_a_missing_kernel_from_backports(kernel) -> None:
    installer, _release, use = kernel
    fake = use(FakeKernelApt(""))

    installer.ensure_backports_kernel()

    install = ["install", "-y", "-q", "-t", "trixie-backports", "linux-image-amd64"]
    assert install in fake.apt_calls()


def test_refuses_when_apt_leaves_the_stable_kernel(kernel) -> None:
    # e.g. backports is missing from the sources: apt exits 0, nothing moves.
    installer, _release, use = kernel
    use(FakeKernelApt(STABLE, after=STABLE))

    with pytest.raises(InstallError, match="still 6.12.111-1"):
        installer.ensure_backports_kernel()


def test_refuses_when_apt_fails(kernel) -> None:
    installer, _release, use = kernel
    use(FakeKernelApt(STABLE, code=100))

    with pytest.raises(InstallError, match="apt-get update failed"):
        installer.ensure_backports_kernel()


def test_refuses_on_a_non_debian_host(kernel) -> None:
    installer, release, use = kernel
    release.write_text("ID=ubuntu\nVERSION_CODENAME=resolute\n", encoding="utf-8")
    fake = use(FakeKernelApt(STABLE))

    with pytest.raises(InstallError, match="Debian"):
        installer.ensure_backports_kernel()
    assert fake.calls == []


def _ctx(tmp_path: Path, **deploy_env: str) -> InstallContext:
    return InstallContext(
        host="vm",
        script_dir=tmp_path,
        build_dir=tmp_path,
        env={},
        deploy_env={"BASE_PACKAGES": "vim", **deploy_env},
        file_map={},
        force_update=False,
    )


@pytest.mark.parametrize(("flag", "moved"), [("true", True), ("false", False), ("", False)])
def test_install_moves_the_kernel_only_when_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, moved: bool
) -> None:
    installer = load_installer()
    monkeypatch.setattr(installer.packages, "ensure", lambda *_a, **_k: None)
    calls: list[bool] = []
    monkeypatch.setattr(installer, "ensure_backports_kernel", lambda: calls.append(True))

    installer.install(_ctx(tmp_path, BACKPORTS_KERNEL=flag))

    assert bool(calls) is moved
