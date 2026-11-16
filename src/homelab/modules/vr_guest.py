"""vr-guest: the guest-side setup of the VR VM (solar, Bazzite), freender/homelab-ops#398.

Replaces the retired `pve-gpu-passthrough/linux/setup.sh`: the `base` and `metrics`
stages (homelab-ops#400; design in #399) and the `vr` stage (#401).

Every destination, mode, pinned release and expected value is decided here and passed
down: the file map and env for the installer, and `check.json` for `solar-check`, the
permanent read-only check that `solar-metrics` publishes and the installer runs as its
verify step. The installer reads `check.json` too, for the SteamVR settings it applies.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, connection_for_host, run_module_deploy, write_file_map
from ..output import print_sub
from ..ssh import build_files, diff_many

MODULE_DIR = "vr-guest"
REMOTE_ROOT = "/tmp/homelab-vr-guest"
INSTALLER = "scripts/install.py"

# The OS-specific layer is one table plus the installer's `bazzite_*` functions, so a
# future `steamos` adds a row here and swaps those (homelab-ops#399 decision 6).
SUPPORTED_TYPES = {
    # Fedora Atomic keeps homes under /var/home (/home is a symlink to it).
    "bazzite": {"home_root": "/var/home"},
}

KARGS = ("nvidia-modeset.conceal_vrr_caps=1", "bluetooth.disable_ertm=1")

NODE_EXPORTER_VERSION = "1.12.1"
NODE_EXPORTER_URL = (
    "https://github.com/prometheus/node_exporter/releases/download/"
    f"v{NODE_EXPORTER_VERSION}/node_exporter-{NODE_EXPORTER_VERSION}.linux-amd64.tar.gz"
)
NODE_EXPORTER_SHA = "b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63"

# The vr stage's pinned BnuuySolutions releases: env prefix -> (repo, tag, zip asset,
# its sha256). Verified against the files solar ran on 2026-11-15.
VR_RELEASES = {
    "SVLF": (
        "SteamVRLinuxFixes", "v0.2.1", "VK_LAYER_BNUUY_steamvr_linux_fixes.zip",
        "af649a5aa45b732aa29989c401d23d9088f013c105c1d94893d77192addb6d05",
    ),
    "IGNITION": (
        "Ignition", "v1.1.0", "Ignition-Linux-Windows.zip",
        "2637bb32c3e9d4ce2f8d8af4f1eaa7f2affe4c4f544868988ba4fd7da9e0f7ec",
    ),
    "PSVR2TK": (
        "PSVR2Toolkit", "v1.0.0-experimental-3", "PSVR2TK-win64-Ignition.zip",
        "306e6bf9465b9266233db69e0c2175056b8cbed5dba0024e78dd056dbb0c6f67",
    ),
    "UNITYSETUP": (
        "PSVR2Toolkit.UnitySetup", "v1.1.0", "PSVR2Toolkit.UnitySetup-Linux.zip",
        "5e223773e6615f71b9496393ae9e1d74df43cab2a30e67e1a1b389e4cc8ad7da",
    ),
}
# driver_playstation_vr2.dll inside the PSVR2TK zip; bump it together with that release.
PSVR2TK_DLL_SHA = "b456e89433ad8dbe02719e215409912bb26fb9bf062d8528bb29174ea3d24583"

# Merged into steamvr.vrsettings' "steamvr" section with SteamVR quit (it rewrites the
# file on exit). 90 Hz at 118% manual supersampling is the settled Alyx tuning.
STEAMVR_SETTINGS = {
    "enableLinuxVulkanAsync": True,
    "useFacetRenderer": True,
    "showMirrorView": False,
    "supersampleManualOverride": True,
    "supersampleScale": 1.18,
}

LIBEXEC = "/usr/local/libexec"
CHECK_PATH = f"{LIBEXEC}/solar-check"
MANIFEST_NAME = "check.json"
MANIFEST_PATH = "/usr/local/share/vr-guest/check.json"

# (repo path under configs/etc/, mode). The check id of each is "/etc/<path>".
ETC_FILES = (
    ("ssh/sshd_config.d/10-key-only.conf", "644"),
    ("plasmalogin.conf.d/autologin.conf", "644"),
    ("udev/rules.d/70-xrhardware.rules", "644"),
    ("udev/rules.d/71-psvr2-steamvr.rules", "644"),
    ("systemd/system/node_exporter.service", "644"),
    ("systemd/system/solar-metrics.service", "644"),
    ("systemd/system/solar-metrics.timer", "644"),
)
# (repo path under files/home/, mode). Destination and check id are relative to $HOME.
HOME_FILES = (
    (".local/bin/psvr2-steamvr", "755"),
    (".local/bin/psvr2-audio", "755"),
    (".config/systemd/user/psvr2-steamvr.service", "644"),
)
# (repo name under files/libexec/, check id or None when the check does not compare it).
LIBEXEC_FILES = (
    ("solar-metrics", "solar_metrics_script"),
    ("solar-check", None),
)


def deploy(
    root: Path,
    requested_host: str,
    dry_run: bool,
    force: bool,
    session: DeploySession,
) -> int:
    return run_module_deploy(
        root,
        requested_host,
        MODULE_DIR,
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda _supported, hosts: validate(root, hosts),
    )


def validate(root: Path, hosts: list[str]) -> None:
    required = [root / MODULE_DIR / INSTALLER]
    required += [source for source, _ in module_sources(root)]
    for path in required:
        if not path.is_file():
            raise ValueError(f"missing module file: {path}")
    for host in hosts:
        host_type(root, host)
        desktop_user(root, host)


def host_type(root: Path, host: str) -> str:
    value = str(default_registry(root).get(host, "config.type", ""))
    if value not in SUPPORTED_TYPES:
        raise ValueError(
            f"{MODULE_DIR} supports config.type {', '.join(sorted(SUPPORTED_TYPES))} only; "
            f"{host} is {value or 'untyped'}"
        )
    return value


def desktop_user(root: Path, host: str) -> str:
    """The auto-login user whose session, home and Steam library this sets up.

    `config.ssh_config.user`, never `config.user`: the deploy user is root.
    """
    user = str(default_registry(root).get(host, "config.ssh_config.user", ""))
    if not user or user == "root":
        raise ValueError(f"{host}: {MODULE_DIR} needs config.ssh_config.user (the desktop user)")
    return user


def module_sources(root: Path) -> list[tuple[Path, str]]:
    """(repo source, build name) of every file installed, in install order."""
    module = root / MODULE_DIR
    sources = [(module / "configs" / "etc" / path, f"etc/{path}") for path, _ in ETC_FILES]
    sources += [(module / "files" / "home" / path, f"home/{path}") for path, _ in HOME_FILES]
    sources += [
        (module / "files" / "libexec" / name, f"libexec/{name}") for name, _ in LIBEXEC_FILES
    ]
    return sources


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_specs(home: str) -> tuple[FileSpec, ...]:
    specs = [FileSpec(f"etc/{path}", f"/etc/{path}", mode) for path, mode in ETC_FILES]
    specs += [FileSpec(f"home/{path}", f"{home}/{path}", mode) for path, mode in HOME_FILES]
    specs += [FileSpec(f"libexec/{name}", f"{LIBEXEC}/{name}", "755") for name, _ in LIBEXEC_FILES]
    specs.append(FileSpec(MANIFEST_NAME, MANIFEST_PATH, "644"))
    return tuple(specs)


def check_manifest(root: Path, host: str, home: str) -> dict[str, object]:
    module = root / MODULE_DIR
    files = [
        {"id": f"/etc/{path}", "path": f"/etc/{path}",
         "sha256": sha256(module / "configs" / "etc" / path)}
        for path, _ in ETC_FILES
    ]
    files += [
        {"id": f"home/{path}", "path": f"{home}/{path}",
         "sha256": sha256(module / "files" / "home" / path)}
        for path, _ in HOME_FILES
    ]
    files += [
        {"id": check_id, "path": f"{LIBEXEC}/{name}",
         "sha256": sha256(module / "files" / "libexec" / name)}
        for name, check_id in LIBEXEC_FILES
        if check_id
    ]
    return {
        "hostname": host,
        "home": home,
        "kargs": list(KARGS),
        "node_exporter_version": NODE_EXPORTER_VERSION,
        "psvr2toolkit_dll_sha256": PSVR2TK_DLL_SHA,
        "steamvr_settings": STEAMVR_SETTINGS,
        "files": files,
    }


def render_build(root: Path, host: str) -> tuple[Path, tuple[FileSpec, ...]]:
    os_type = host_type(root, host)
    user = desktop_user(root, host)
    home = f"{SUPPORTED_TYPES[os_type]['home_root']}/{user}"
    build_dir = root / MODULE_DIR / "build" / host
    prepare_build_dir(build_dir)

    for source, name in module_sources(root):
        (build_dir / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, build_dir / name)

    manifest = check_manifest(root, host, home)
    (build_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    releases = {}
    for prefix, (repo, tag, asset, digest) in VR_RELEASES.items():
        releases[f"{prefix}_URL"] = (
            f"https://github.com/BnuuySolutions/{repo}/releases/download/{tag}/{asset}"
        )
        releases[f"{prefix}_SHA"] = digest
    write_env_file(
        build_dir / "env",
        {
            "OS_TYPE": os_type,
            "HOSTNAME": host,
            "DESKTOP_USER": user,
            "DESKTOP_HOME": home,
            "KARGS": " ".join(KARGS),
            "NODE_EXPORTER_VERSION": NODE_EXPORTER_VERSION,
            "NODE_EXPORTER_URL": NODE_EXPORTER_URL,
            "NODE_EXPORTER_SHA": NODE_EXPORTER_SHA,
            "CHECK_PATH": CHECK_PATH,
            **releases,
        },
    )
    specs = file_specs(home)
    write_file_map(build_dir, specs)
    return build_dir, specs


def deploy_host(root: Path, host: str, dry_run: bool, force: bool) -> None:
    build_dir, specs = render_build(root, host)

    connection = connection_for_host(root, host)
    print_sub("Comparing with remote files...")
    pairs = [(build_dir / spec.build_name, spec.remote_path) for spec in specs]
    for message in diff_many(connection, pairs):
        print_sub(message)

    if dry_run:
        print_sub(f"[DRY-RUN] Would deploy {MODULE_DIR} to {host}:{REMOTE_ROOT}/")
        print_sub("Build files:")
        for file_name in build_files(build_dir):
            print_sub(f"    {file_name}")
        return

    stage_and_run_remote_installer(
        root,
        connection,
        REMOTE_ROOT,
        [
            (build_dir, f"{REMOTE_ROOT}/build/{host}"),
            (root / MODULE_DIR / "scripts", f"{REMOTE_ROOT}/scripts"),
        ],
        INSTALLER,
        host,
        env=force_env(force),
        require_root=True,
        interpreter="python3",
        remote_subdirs=("build", "scripts", "src"),
    )
