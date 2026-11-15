#!/usr/bin/env python3
"""Remote installer for the nvidia-gpu module (freender/homelab-ops#300).

Puts a headless NVIDIA compute stack on a GPU passthrough VM: NVIDIA's CUDA apt
repo, the driver pinned to one branch, the open kernel modules via DKMS, and the
NVIDIA Container Toolkit for `docker run --gpus`.

Worth knowing before reading:

* **One repo, one key.** NVIDIA's `debian<N>` CUDA repo carries the driver *and*
  `nvidia-container-toolkit`, so there is no second libnvidia-container source.
  The source line is written here, from `/etc/os-release`, like docker's.
* **The pin goes in first, in its own apt call.** `nvidia-driver-pinning-<branch>`
  drops an apt preferences file holding every driver package at `<branch>*` with
  priority 1000. Installed in the same transaction as the driver, apt would have
  resolved the newest branch before the pin existed.
* **Compute only.** `nvidia-driver-cuda` + `nvidia-kernel-open-dkms`, not the
  `nvidia-open` metapackage, which pulls Xorg, VDPAU and nvidia-settings. Open
  modules are NVIDIA's recommendation from Turing on.
* **The packages blacklist nouveau themselves.** `nvidia-kernel-support` ships
  `/etc/modprobe.d/nvidia.conf` and rebuilds the initramfs in its postinst, so
  this module writes no modprobe file of its own.
* **`linux-headers-amd64` is what keeps the driver across kernel updates.** DKMS
  can only build for a kernel whose headers are installed; the metapackage makes
  the daily dist-upgrade bring them along with every new kernel image.
* **Docker's `daemon.json` is not touched.** It belongs to the docker module, and
  `--gpus` works through the toolkit's runtime hook without registering an
  `nvidia` runtime there.
* **Never reboots.** A first install, or a driver update, leaves nouveau or the
  old module loaded until the next boot; the deploy says so and stops there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from homelab_install import env, files, log, osinfo, packages, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

BRANCH = "NVIDIA_BRANCH"
KEYRING_FILE = "nvidia-cuda.asc"
REPO_BASE = "https://developer.download.nvidia.com/compute/cuda/repos"
# dpkg's architecture name, and the directory NVIDIA publishes it under.
REPO_ARCH = {"amd64": "x86_64"}

HEADERS = ("linux-headers-amd64",)
DRIVER_PACKAGES = ("nvidia-driver-cuda", "nvidia-kernel-open-dkms")
TOOLKIT_PACKAGES = ("nvidia-container-toolkit",)
VERSIONED_PACKAGE = "nvidia-kernel-open-dkms"

# Module-level so tests can rebind them under tmp_path.
OS_RELEASE = osinfo.OS_RELEASE
SOURCE_LIST = "/etc/apt/sources.list.d/nvidia-cuda.list"
LOADED_VERSION = "/proc/driver/nvidia/version"

# Indirection point for tests, same pattern as `homelab_install.packages._run`.
_run = subprocess.run


def pin_package(branch: str) -> str:
    return f"nvidia-driver-pinning-{branch}"


def preflight(ctx: InstallContext) -> tuple[str, str]:
    """Every refusal, before any write. Returns (branch, apt source line)."""
    env.require(ctx, BRANCH)
    branch = ctx.env[BRANCH].strip()
    if not branch.isdigit():
        raise InstallError(f"{BRANCH} must be a driver branch number, got {branch!r}")
    return branch, apt_source_line(ctx)


def apt_source_line(ctx: InstallContext) -> str:
    """The `deb` line for this host; `signed-by` is wherever the file map puts the key."""
    try:
        keyring, _mode = ctx.file_map[KEYRING_FILE]
    except KeyError as exc:
        raise InstallError(f"missing file-map entry: {KEYRING_FILE}") from exc
    release = osinfo.os_release(OS_RELEASE)
    distro = release.get("ID", "")
    version = release.get("VERSION_ID", "")
    if distro != "debian" or not version.isdigit():
        raise InstallError(
            f"unsupported OS for the NVIDIA CUDA repo: ID={distro!r} VERSION_ID={version!r}"
        )
    result = _run(["dpkg", "--print-architecture"], check=False, capture_output=True, text=True)
    arch = (result.stdout or "").strip()
    if result.returncode != 0 or arch not in REPO_ARCH:
        raise InstallError(f"unsupported architecture for the NVIDIA CUDA repo: {arch!r}")
    return f"deb [signed-by={keyring}] {REPO_BASE}/debian{version}/{REPO_ARCH[arch]}/ /\n"


def ensure_apt_source(ctx: InstallContext, line: str) -> None:
    staged = ctx.build_dir / "nvidia-cuda.list"
    staged.write_text(line, encoding="utf-8")
    key = files.install(ctx, KEYRING_FILE)
    source = files.install_from(ctx, staged, SOURCE_LIST, "644")
    if key or source:
        packages.sources_changed(ctx)


def installed_version(package: str) -> str:
    result = _run(
        ["dpkg-query", "-W", "-f=${Version}", package],
        check=False,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def check_branch(branch: str) -> str:
    """Refuse a driver outside the pinned branch; return its upstream version.

    Catches a pin that never took (wrong install order, a second pinning file)
    and a branch changed in hosts.conf that apt has not moved to yet.
    """
    version = installed_version(VERSIONED_PACKAGE)
    if not version.startswith(f"{branch}."):
        raise InstallError(
            f"{VERSIONED_PACKAGE} is {version or 'absent'}, outside pinned branch {branch}; "
            "run apt-get dist-upgrade to move it onto the pin"
        )
    log.ok(f"NVIDIA driver {version} (branch {branch})")
    return version.split("-", 1)[0]


def loaded_version() -> str:
    """The running kernel module's version, or "" when none is loaded."""
    path = Path(LOADED_VERSION)
    if not path.is_file():
        return ""
    for word in path.read_text(encoding="utf-8").split():
        parts = word.split(".")
        if len(parts) >= 2 and all(part.isdigit() for part in parts):
            return word
    return ""


def report_loaded(expected: str) -> None:
    loaded = loaded_version()
    if loaded == expected:
        log.sub(f"Kernel module {loaded} loaded")
    elif not loaded:
        log.warn("NVIDIA kernel module not loaded; reboot to load it (this module never reboots)")
    else:
        log.warn(f"Kernel module {loaded} still loaded, {expected} installed; reboot to switch")


def install(ctx: InstallContext) -> None:
    branch, source_line = preflight(ctx)
    log.header("NVIDIA GPU")

    log.action("NVIDIA CUDA apt repo")
    ensure_apt_source(ctx, source_line)

    log.action(f"Driver, pinned to branch {branch}")
    packages.ensure(ctx, pin_package(branch))
    packages.ensure(ctx, *HEADERS, *DRIVER_PACKAGES)
    expected = check_branch(branch)

    log.action("NVIDIA Container Toolkit")
    packages.ensure(ctx, *TOOLKIT_PACKAGES)

    report_loaded(expected)


if __name__ == "__main__":
    run(install, "NVIDIA GPU")
