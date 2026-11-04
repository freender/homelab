"""nvidia-gpu: orchestrator render and remote installer (freender/homelab-ops#300).

Destinations are rebound under `tmp_path`; apt, dpkg and dpkg-query are faked.
Nothing here touches `/etc`, `/proc` or a real package.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

from homelab.hosts import default_registry
from homelab.modules import MODULE_ORDER, nvidia_gpu
from homelab_install import packages
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError
from homelab_install.main import _parse_env_file, _parse_file_map

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "nvidia-gpu" / "scripts" / "install.py"
CUDA_SOURCE = (
    "deb [signed-by={key}] "
    "https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/ /\n"
)

HOSTS = """\
gpu:
  config:
    type: ubuntu
    hostname: gpu.internal
    user: root
    sshkey: infra
  features:
    nvidia-gpu:{body}
"""


def write_hosts(tmp_path: Path, body: str) -> Path:
    (tmp_path / "hosts.conf").write_text(HOSTS.format(body=body), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_branch_is_read_as_a_string(tmp_path: Path) -> None:
    root = write_hosts(tmp_path, "\n      branch: 595")
    assert nvidia_gpu.driver_branch(root, "gpu") == "595"


@pytest.mark.parametrize("body", ["", "\n      branch: latest", "\n      branch: '595.112'"])
def test_branch_is_required_and_numeric(tmp_path: Path, body: str) -> None:
    root = write_hosts(tmp_path, body)
    with pytest.raises(ValueError, match="nvidia-gpu.branch"):
        nvidia_gpu.driver_branch(root, "gpu")


def test_validate_checks_every_targeted_hosts_branch(tmp_path: Path) -> None:
    root = write_hosts(tmp_path, "")
    (root / "nvidia-gpu").symlink_to(REPO_ROOT / "nvidia-gpu")
    with pytest.raises(ValueError, match="branch"):
        nvidia_gpu.validate(root, ["gpu"])


def test_validate_requires_the_key(tmp_path: Path) -> None:
    root = write_hosts(tmp_path, "\n      branch: 595")
    (root / "nvidia-gpu" / "configs").mkdir(parents=True)
    with pytest.raises(ValueError, match="nvidia-cuda.asc"):
        nvidia_gpu.validate(root, ["gpu"])


def render(tmp_path: Path, branch: str = "595") -> Path:
    (tmp_path / "nvidia-gpu").mkdir()
    (tmp_path / "nvidia-gpu" / "configs").symlink_to(REPO_ROOT / "nvidia-gpu" / "configs")
    return nvidia_gpu.render_build(tmp_path, "gpu", branch)


def test_render_ships_the_key_the_map_installs_and_the_branch(tmp_path: Path) -> None:
    build_dir = render(tmp_path, "610")

    file_map = _parse_file_map(build_dir / "file-map.conf")
    assert file_map == {"nvidia-cuda.asc": ("/etc/apt/keyrings/nvidia-cuda.asc", "644")}
    assert (build_dir / "nvidia-cuda.asc").read_bytes() == (
        REPO_ROOT / "nvidia-gpu" / "configs" / "nvidia-cuda.asc"
    ).read_bytes()
    assert _parse_env_file(build_dir / "env") == {"NVIDIA_BRANCH": "610"}


def test_the_shipped_key_is_nvidias_cuda_repo_key() -> None:
    """Pinned so a swapped key is a test failure, not a silent trust change."""
    key = (REPO_ROOT / "nvidia-gpu" / "configs" / "nvidia-cuda.asc").read_bytes()
    assert hashlib.sha256(key).hexdigest() == (
        "09edb5196bca272c091633d4e04dd6c8f3a07278834868220aa669c895f6cfd7"
    )


def test_runs_after_docker_and_before_its_stacks() -> None:
    position = MODULE_ORDER.index("nvidia-gpu")
    assert MODULE_ORDER.index("docker") < position < MODULE_ORDER.index("docker-stacks")


def test_only_glint_carries_the_feature_and_it_runs_docker() -> None:
    registry = default_registry(REPO_ROOT)
    assert registry.list_hosts(feature="nvidia-gpu") == ["glint"]
    assert "glint" in registry.list_hosts(feature="docker")
    assert nvidia_gpu.driver_branch(REPO_ROOT, "glint") == "595"


def test_orchestrator_stages_the_python_installer() -> None:
    assert nvidia_gpu.INSTALLER == "scripts/install.py"
    assert nvidia_gpu.INTERPRETER == "python3"
    assert (REPO_ROOT / "nvidia-gpu" / nvidia_gpu.INSTALLER).is_file()


def test_key_name_agrees_across_the_wire() -> None:
    assert load_installer().KEYRING_FILE == nvidia_gpu.KEYRING_SPEC.build_name


# ---------------------------------------------------------------------------
# Remote installer
# ---------------------------------------------------------------------------


def load_installer():
    spec = importlib.util.spec_from_file_location("nvidia_gpu_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApt:
    """dpkg, dpkg-query and apt-get. An install records the packages and gives the
    versioned driver package `driver_version`."""

    def __init__(self, installed: set[str], driver_version: str) -> None:
        self.installed = set(installed)
        self.driver_version = driver_version
        self.arch = "amd64"
        self.installs: list[list[str]] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["dpkg", "--print-architecture"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{self.arch}\n")
        if argv[:2] == ["dpkg-query", "-W"]:
            package = argv[-1]
            if package not in self.installed:
                return subprocess.CompletedProcess(argv, 1, stdout="")
            if argv[2] == "-f=${Version}":
                return subprocess.CompletedProcess(argv, 0, stdout=self.driver_version)
            return subprocess.CompletedProcess(argv, 0, stdout="install ok installed")
        if argv[0] == "apt-get" and "install" in argv:
            new = argv[argv.index("-q") + 1 :]
            self.installs.append(new)
            self.installed.update(new)
        return subprocess.CompletedProcess(argv, 0, stdout="")


class Host:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        installed: set[str] | None = None,
        driver_version: str = "595.112.07-1",
        loaded: str | None = None,
    ) -> None:
        self.installer = load_installer()
        root = tmp_path / "hostfs"
        self.os_release = root / "etc" / "os-release"
        self.os_release.parent.mkdir(parents=True)
        self.os_release.write_text('ID=debian\nVERSION_ID="13"\n', encoding="utf-8")
        self.source_list = root / "etc/apt/sources.list.d/nvidia-cuda.list"
        self.keyring = root / "etc/apt/keyrings/nvidia-cuda.asc"
        self.proc_version = root / "proc/driver/nvidia/version"
        if loaded is not None:
            self.proc_version.parent.mkdir(parents=True)
            self.proc_version.write_text(
                f"NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  {loaded}  "
                "Release Build  (dvs-builder@U16-I1-N08-12-1)  Mon Oct 20 2026\n"
                "GCC version:  gcc version 14.2.0 (Debian 14.2.0-19)\n",
                encoding="utf-8",
            )
        monkeypatch.setattr(self.installer, "OS_RELEASE", str(self.os_release))
        monkeypatch.setattr(self.installer, "SOURCE_LIST", str(self.source_list))
        monkeypatch.setattr(self.installer, "LOADED_VERSION", str(self.proc_version))

        self.apt = FakeApt(installed or set(), driver_version)
        monkeypatch.setattr(packages, "_run", self.apt)
        monkeypatch.setattr(packages, "_apt_updated", False)
        monkeypatch.setattr(self.installer, "_run", self.apt)

        self.build_dir = tmp_path / "remote" / "build" / "gpu"
        self.build_dir.mkdir(parents=True)
        (self.build_dir / "nvidia-cuda.asc").write_text("KEY\n", encoding="utf-8")
        self.ctx = InstallContext(
            host="gpu",
            script_dir=tmp_path / "remote",
            build_dir=self.build_dir,
            env={"NVIDIA_BRANCH": "595"},
            deploy_env={},
            file_map={"nvidia-cuda.asc": (str(self.keyring), "644")},
            force_update=False,
        )

    def warnings(self, capsys) -> str:
        return capsys.readouterr().out


ALL_PACKAGES = {
    "nvidia-driver-pinning-595",
    "linux-headers-amd64",
    "nvidia-driver-cuda",
    "nvidia-kernel-open-dkms",
    "nvidia-container-toolkit",
}


def test_fresh_host_gets_source_then_pin_alone_then_driver_then_toolkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)

    host.installer.install(host.ctx)

    assert host.apt.installs == [
        ["nvidia-driver-pinning-595"],
        ["linux-headers-amd64", "nvidia-driver-cuda", "nvidia-kernel-open-dkms"],
        ["nvidia-container-toolkit"],
    ]
    assert host.source_list.read_text(encoding="utf-8") == CUDA_SOURCE.format(key=host.keyring)
    assert host.keyring.read_text(encoding="utf-8") == "KEY\n"


def test_converged_host_installs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host(tmp_path, monkeypatch, installed=ALL_PACKAGES, loaded="595.112.07")
    host.installer.install(host.ctx)  # first run writes the source and key
    host.apt.calls.clear()

    host.installer.install(host.ctx)

    assert host.apt.installs == []
    assert not any(call[0] == "apt-get" for call in host.apt.calls)


def test_never_writes_docker_config_or_reboots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    host.installer.install(host.ctx)
    commands = {call[0] for call in host.apt.calls}
    assert commands <= {"dpkg", "dpkg-query", "apt-get"}
    assert not any("nvidia-ctk" in part for call in host.apt.calls for part in call)


def test_refuses_a_driver_off_the_pinned_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch, driver_version="620.70.06-1")

    with pytest.raises(InstallError, match="620.70.06-1, outside pinned branch 595"):
        host.installer.install(host.ctx)


def test_branch_prefix_needs_the_dot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`5950.1` must not pass for branch 595."""
    host = Host(tmp_path, monkeypatch, driver_version="5950.1-1")
    with pytest.raises(InstallError, match="outside pinned branch"):
        host.installer.install(host.ctx)


@pytest.mark.parametrize(
    ("release", "message"),
    [
        ('ID=ubuntu\nVERSION_ID="24.04"\n', "ID='ubuntu'"),
        ("ID=debian\n", "VERSION_ID=''"),
    ],
)
def test_refuses_unsupported_os_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: str, message: str
) -> None:
    host = Host(tmp_path, monkeypatch)
    host.os_release.write_text(release, encoding="utf-8")

    with pytest.raises(InstallError, match=message):
        host.installer.install(host.ctx)
    assert not host.source_list.exists()
    assert host.apt.installs == []


def test_refuses_an_unpublished_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    host.apt.arch = "arm64"
    with pytest.raises(InstallError, match="arm64"):
        host.installer.install(host.ctx)
    assert not host.keyring.exists()


@pytest.mark.parametrize("branch", ["", "latest"])
def test_refuses_a_bad_branch_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    host = Host(tmp_path, monkeypatch)
    host.ctx.env["NVIDIA_BRANCH"] = branch
    with pytest.raises(InstallError):
        host.installer.install(host.ctx)
    assert host.apt.calls == []


def test_a_new_source_makes_the_next_install_refresh_the_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    monkeypatch.setattr(packages, "_apt_updated", True)  # lists fetched before the repo existed

    host.installer.install(host.ctx)

    assert ["apt-get", "-o", "DPkg::Lock::Timeout=600", "update", "-qq"] in host.apt.calls


@pytest.mark.parametrize(
    ("loaded", "expected"),
    [
        (None, "not loaded; reboot"),
        ("595.95.05", "595.95.05 still loaded, 595.112.07 installed"),
    ],
)
def test_warns_when_a_reboot_is_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, loaded, expected: str
) -> None:
    host = Host(tmp_path, monkeypatch, loaded=loaded)
    host.installer.install(host.ctx)
    assert expected in host.warnings(capsys)


def test_no_warning_when_the_installed_module_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    host = Host(tmp_path, monkeypatch, loaded="595.112.07")
    host.installer.install(host.ctx)
    out = host.warnings(capsys)
    assert "Kernel module 595.112.07 loaded" in out
    assert "reboot" not in out
