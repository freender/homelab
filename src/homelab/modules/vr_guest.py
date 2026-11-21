"""vr-guest: the guest-side setup of the VR VM (solar, Bazzite), freender/homelab-ops#398.

Replaces the retired `pve-gpu-passthrough/linux/setup.sh`: the `base` and `metrics`
stages (homelab-ops#400; design in #399) and the `vr` stage (#401). The `vector`
stage ships the journal to VictoriaLogs (#403, design in #402) from vector-host's
config render. The `bluez` stage builds and installs the patched bluetoothd (#405).
The `goxlr` stage installs goxlr-utility for the GoXLR mixer moved over from exo.
The `sunshine` stage installs Sunshine, the Moonlight host the Apple TV streams from.

Every destination, mode, pinned release and expected value is decided here and passed
down: the file map and env for the installer, and `check.json` for `solar-check`, the
permanent read-only check that `solar-metrics` publishes and the installer runs as its
verify step. The installer reads `check.json` too, for the SteamVR settings it applies.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from ..build import write_env_file
from ..deploy import DeploySession, force_env, prepare_build_dir, stage_and_run_remote_installer
from ..hosts import default_registry
from ..module_support import FileSpec, connection_for_host, run_module_deploy, write_file_map
from ..output import print_sub
from ..ssh import build_files, diff_many
from . import vector_host

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

# Nobody on solar is an admin: games run untrusted code as the desktop user, and
# wheel is root-equivalent through polkit alone (rpm-ostree package installs,
# firewall, system Flatpaks with no password). Root is SSH-only (`infra` key).
ADMIN_GROUP = "wheel"
# The hand-placed NOPASSWD file the desktop user had before this was managed.
RETIRED_SUDOERS = ("/etc/sudoers.d/90-freender-nopasswd",)

NODE_EXPORTER_VERSION = "1.12.1"
NODE_EXPORTER_URL = (
    "https://github.com/prometheus/node_exporter/releases/download/"
    f"v{NODE_EXPORTER_VERSION}/node_exporter-{NODE_EXPORTER_VERSION}.linux-amd64.tar.gz"
)
NODE_EXPORTER_SHA = "b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63"

# The release the apt hosts run (vector-host); bumped by hand, nothing watches it.
VECTOR_VERSION = "0.61.0"
VECTOR_URL = (
    "https://github.com/vectordotdev/vector/releases/download/"
    f"v{VECTOR_VERSION}/vector-{VECTOR_VERSION}-x86_64-unknown-linux-gnu.tar.gz"
)
VECTOR_SHA = "84a27c3ad6b772fdb05bb76ea01a31498bfc8f5e71f32cc78a0d96841f1aa827"
# Rendered by vector-host into build/<host>/vector/; tenant 0, never shadow.
VECTOR_DIR = "vector"
VECTOR_FILES = (
    # (build name, destination, mode, check id)
    (vector_host.CONFIG_NAME, vector_host.CONFIG_PATH, "644", vector_host.CONFIG_PATH),
    (vector_host.WRAPPER_NAME, vector_host.WRAPPER_PATH, "755", "vector_wrapper"),
)
# Steam's and SteamVR's logs Vector ships beside the journal, under the desktop home:
# the ones the steam skill debugs from. The unit's CAP_DAC_READ_SEARCH reads them.
STEAM_LOG_DIR = ".local/share/Steam/logs"
STEAM_LOGS = (
    "vrserver.txt", "vrcompositor.txt", "vrcompositor-linux.txt",
    "console_log.txt", "connection_log.txt", "webhelper.txt", "webhelper_vr.txt",
)

# goxlr-utility's RPM, unpacked rather than layered: its four binaries go to
# /usr/local/bin and its udev rule ships in ETC_FILES. Its user data (profiles,
# mic profiles, samples) is the user's, not the repo's: copied by hand.
GOXLR_VERSION = "1.2.4"
GOXLR_URL = (
    "https://github.com/GoXLR-on-Linux/goxlr-utility/releases/download/"
    f"v{GOXLR_VERSION}/goxlr-utility-{GOXLR_VERSION}-1.x86_64.rpm"
)
GOXLR_SHA = "a3006b5536f98d162904c41a407c66810df2509ef47bb6457cbc8f2a05c297ea"

# Sunshine from the LizardByte tap's Homebrew build, in /home/linuxbrew, which image
# updates never touch. Its KMS capture needs no KDE screen-share prompt (the Flatpak
# cannot do KMS), and the RPM hardcodes /usr/share/sunshine, so it would need
# layering. Brew only installs the tap's current formula: the stage refuses any
# other release, so bump this when the tap moves. Steam Link was tried first
# (homelab-ops#416): ~14 FPS desktop capture.
# sunshine-beta, the tap's pre-release: 2026.914 (stable) drops the DualSense's
# feedback when Moonlight resumes a running app (fixed upstream in Sunshine#5941,
# with #5818 and newer libvirtualhid). Back to "sunshine" once a stable has them;
# the stage swaps the two (brew will not install both), and the unit's ExecStart
# names the formula's opt path.
SUNSHINE_FORMULA = "sunshine-beta"
SUNSHINE_VERSION = "2026.1118.30014"
SUNSHINE_TAP = "LizardByte/homebrew"
SUNSHINE_UNIT = "app-dev.lizardbyte.app.Sunshine.service"
# (repo name under files/sunshine/, destination relative to $HOME, check id)
SUNSHINE_FILES = (
    ("sunshine.conf", ".config/sunshine/sunshine.conf", "sunshine_conf"),
    (SUNSHINE_UNIT, f".config/systemd/user/{SUNSHINE_UNIT}", "sunshine_unit"),
)

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

# The patched bluetoothd (bluez-eagain/README.md) is built on solar from these inputs.
# BLUEZ_TERRA is the exact `rpm -q terra-bluez` the patch set reproduces; the stage
# refuses any other, and build.sh fetches the bluez commit named in it (bluez_commit).
# Bump it together with the patch set.
BLUEZ_SOURCE = "bluez-eagain"
BLUEZ_TERRA = "terra-bluez-5.87+1.git8750129efca8-2.fc45.x86_64"
BLUEZ_IMAGE = "registry.fedoraproject.org/fedora:45"
BLUEZ_DIR = "bluez"
BLUEZ_DROPIN = "50-eagain-patch.conf"
BLUEZ_DROPIN_PATH = f"/etc/systemd/system/bluetooth.service.d/{BLUEZ_DROPIN}"

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
    ("udev/rules.d/50-goxlr.rules", "644"),
    ("systemd/system/node_exporter.service", "644"),
    ("systemd/system/solar-metrics.service", "644"),
    ("systemd/system/solar-metrics.timer", "644"),
    ("systemd/system/solar-gpu-metrics.service", "644"),
    ("systemd/system/vector.service", "644"),
)
# (repo path under files/home/, mode). Destination and check id are relative to $HOME.
HOME_FILES = (
    (".local/bin/psvr2-steamvr", "755"),
    (".local/bin/psvr2-audio", "755"),
    # The desktop session's env for commands run over SSH (kscreen-doctor, Steam).
    (".local/bin/solar-session", "755"),
    (".config/systemd/user/psvr2-steamvr.service", "644"),
    # What goxlr-utility's own "autostart" toggle writes, so the UI shows it on.
    (".config/autostart/goxlr-daemon.desktop", "644"),
    # -pipewire lets Remote Play (Steam Link) capture the Wayland desktop.
    (".config/autostart/steam.desktop", "644"),
    (".local/share/applications/goxlr-utility.desktop", "644"),
    # Applies at the next WirePlumber start (login); the deploy never restarts it.
    (".config/wireplumber/wireplumber.conf.d/51-disable-acer-audio.conf", "644"),
)
# (repo name under files/libexec/, check id or None when the check does not compare it).
LIBEXEC_FILES = (
    ("solar-metrics", "solar_metrics_script"),
    ("solar-gpu-metrics", "solar_gpu_metrics_script"),
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
    required += vector_host.config_sources(root)
    for path in required:
        if not path.is_file():
            raise ValueError(f"missing module file: {path}")
    if not bluez_patches(root):
        raise ValueError(f"no patches in {root / MODULE_DIR / BLUEZ_SOURCE / 'patches'}")
    bluez_commit(BLUEZ_TERRA)
    vector_host.victoria_logs_base(root)
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
    sources += [
        (module / "files" / "sunshine" / name, f"sunshine/{name}") for name, _, _ in SUNSHINE_FILES
    ]
    sources += [
        (module / BLUEZ_SOURCE / name, f"{BLUEZ_DIR}/{name}")
        for name in ("build.sh", BLUEZ_DROPIN)
    ]
    sources += [
        (patch, f"{BLUEZ_DIR}/patches/{patch.name}") for patch in bluez_patches(root)
    ]
    return sources


def bluez_patches(root: Path) -> list[Path]:
    return sorted((root / MODULE_DIR / BLUEZ_SOURCE / "patches").glob("*.patch"))


def bluez_commit(terra: str) -> str:
    """The bluez/bluez commit a terra-bluez NEVRA is built from (its `.git<hash>-`)."""
    match = re.fullmatch(r"terra-bluez-[^+]+\+\d+\.git([0-9a-f]{7,40})-.+", terra)
    if match is None:
        raise ValueError(f"BLUEZ_TERRA {terra!r} names no bluez git commit")
    return match.group(1)


def bluez_inputs(root: Path) -> str:
    """Fingerprint of everything the bluetoothd build depends on.

    Recorded next to the installed binary; a Podman build is not byte-reproducible,
    so this, not the binary's hash, is what says the build is current.
    """
    source = root / MODULE_DIR / BLUEZ_SOURCE
    digest = hashlib.sha256(f"{BLUEZ_TERRA}\n{BLUEZ_IMAGE}\n".encode())
    for path in (source / "build.sh", *bluez_patches(root)):
        digest.update(f"{path.name}\n".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def steam_log_paths(home: str) -> tuple[str, ...]:
    return tuple(f"{home}/{STEAM_LOG_DIR}/{name}" for name in STEAM_LOGS)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_specs(home: str) -> tuple[FileSpec, ...]:
    specs = [FileSpec(f"etc/{path}", f"/etc/{path}", mode) for path, mode in ETC_FILES]
    specs += [FileSpec(f"home/{path}", f"{home}/{path}", mode) for path, mode in HOME_FILES]
    specs += [FileSpec(f"libexec/{name}", f"{LIBEXEC}/{name}", "755") for name, _ in LIBEXEC_FILES]
    specs += [FileSpec(f"sunshine/{name}", f"{home}/{dest}", "644")
              for name, dest, _ in SUNSHINE_FILES]
    specs += [FileSpec(f"{VECTOR_DIR}/{name}", dest, mode) for name, dest, mode, _ in VECTOR_FILES]
    specs.append(FileSpec(f"{BLUEZ_DIR}/{BLUEZ_DROPIN}", BLUEZ_DROPIN_PATH, "644"))
    specs.append(FileSpec(MANIFEST_NAME, MANIFEST_PATH, "644"))
    return tuple(specs)


def check_manifest(root: Path, host: str, home: str, vector_dir: Path) -> dict[str, object]:
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
    files += [
        {"id": check_id, "path": dest, "sha256": sha256(vector_dir / name)}
        for name, dest, _, check_id in VECTOR_FILES
    ]
    files.append({"id": "bluez_dropin", "path": BLUEZ_DROPIN_PATH,
                  "sha256": sha256(module / BLUEZ_SOURCE / BLUEZ_DROPIN)})
    files += [
        {"id": check_id, "path": f"{home}/{dest}",
         "sha256": sha256(module / "files" / "sunshine" / name)}
        for name, dest, check_id in SUNSHINE_FILES
    ]
    return {
        "hostname": host,
        "home": home,
        "kargs": list(KARGS),
        "admin_group": ADMIN_GROUP,
        "node_exporter_version": NODE_EXPORTER_VERSION,
        "vector_version": VECTOR_VERSION,
        "goxlr_version": GOXLR_VERSION,
        "sunshine_formula": SUNSHINE_FORMULA,
        "sunshine_version": SUNSHINE_VERSION,
        "psvr2toolkit_dll_sha256": PSVR2TK_DLL_SHA,
        "bluez_inputs": bluez_inputs(root),
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

    vector_dir = build_dir / VECTOR_DIR
    vector_host.render_config(root, host, vector_dir, shadow=False,
                              steam_logs=steam_log_paths(home))
    manifest = check_manifest(root, host, home, vector_dir)
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
            "ADMIN_GROUP": ADMIN_GROUP,
            "RETIRED_SUDOERS": " ".join(RETIRED_SUDOERS),
            "NODE_EXPORTER_VERSION": NODE_EXPORTER_VERSION,
            "NODE_EXPORTER_URL": NODE_EXPORTER_URL,
            "NODE_EXPORTER_SHA": NODE_EXPORTER_SHA,
            "CHECK_PATH": CHECK_PATH,
            "VECTOR_VERSION": VECTOR_VERSION,
            "VECTOR_URL": VECTOR_URL,
            "VECTOR_SHA": VECTOR_SHA,
            "VECTOR_QUERY_URL": f"{vector_host.victoria_logs_base(root)}/select/logsql/query",
            "VECTOR_TENANT": "0",
            "GOXLR_VERSION": GOXLR_VERSION,
            "GOXLR_URL": GOXLR_URL,
            "GOXLR_SHA": GOXLR_SHA,
            "SUNSHINE_FORMULA": SUNSHINE_FORMULA,
            "SUNSHINE_VERSION": SUNSHINE_VERSION,
            "SUNSHINE_TAP": SUNSHINE_TAP,
            "SUNSHINE_UNIT": SUNSHINE_UNIT,
            "BLUEZ_TERRA": BLUEZ_TERRA,
            "BLUEZ_COMMIT": bluez_commit(BLUEZ_TERRA),
            "BLUEZ_IMAGE": BLUEZ_IMAGE,
            "BLUEZ_INPUTS": bluez_inputs(root),
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
