#!/usr/bin/env python3
"""Remote installer for vr-guest (freender/homelab-ops#400, #401; design in #399).

The `base`, `metrics` and `vr` stages of the retired `setup.sh`, plus `vector`
(#403), run as root. Steps
that need the desktop user's session (`systemctl --user`, `kwriteconfig6 --notify`)
go through `runuser` with that session's bus; with nobody logged in they are skipped
with a warning rather than failing the deploy.

Kernel-argument changes only take effect after a reboot, which this never does:
it reports "reboot required" and leaves the power-cycle to a human, because
someone may be in the headset. For the same reason the `vr` stage is skipped, with
a warning, while SteamVR runs; it never stops SteamVR.

The `vector` stage installs the pinned release tarball, not an rpm (#402), and
checks the staged config and delivery the way vector-host does
(`homelab_install.vector`).

Ends by running `solar-check` as the desktop user, the same check `solar-metrics`
publishes every 15 minutes. A failure this module owns fails the deploy; a
not-yet-booted karg, and the vr items when the vr stage was skipped, only warn.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
import tarfile
import urllib.request
import zipfile
import zlib
from pathlib import Path
from typing import NamedTuple

from homelab_install import env, files, log, run, systemd, vector
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

_run = subprocess.run

SSHD_DROPIN = "etc/ssh/sshd_config.d/10-key-only.conf"
UDEV_RULES = ("etc/udev/rules.d/70-xrhardware.rules", "etc/udev/rules.d/71-psvr2-steamvr.rules")
SYSTEM_UNITS = (
    "etc/systemd/system/node_exporter.service",
    "etc/systemd/system/solar-metrics.service",
    "etc/systemd/system/solar-metrics.timer",
)
ETC_OTHER = ("etc/plasmalogin.conf.d/autologin.conf",)
NODE_EXPORTER = "/usr/local/bin/node_exporter"
VECTOR = "/usr/local/bin/vector"
VECTOR_MEMBER = "vector-x86_64-unknown-linux-gnu/bin/vector"
VECTOR_UNIT = "vector.service"
VECTOR_UNIT_FILE = "etc/systemd/system/vector.service"
VECTOR_CONFIG = "vector/vector.yaml"
VECTOR_TESTS = "vector/vector.test.yaml"
VECTOR_WRAPPER = "vector/journalctl-catchup"
VECTOR_MARKER_TAG = "vr-guest"
TEXTFILE_DIR = "/var/lib/node_exporter/textfile"
CACHE_DIR = Path("/var/cache/vr-guest")
STEAM_MOUNT = "/var/mnt/steam"
STEAM_FSTAB = f"LABEL=steam {STEAM_MOUNT} ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2"

KDE_SETTINGS = (
    ("powerdevilrc", ("AC", "Display"), "DimDisplayWhenIdle", "false"),
    ("powerdevilrc", ("AC", "Display"), "TurnOffDisplayWhenIdle", "false"),
    ("kscreenlockerrc", ("Daemon",), "Autolock", "false"),
    ("kscreenlockerrc", ("Daemon",), "LockOnResume", "false"),
)

# Check ids a base/metrics run converges; a FAIL on one of these after the run is
# this module's failure. The vr stage adds VR_CHECKS when it ran; booted kargs and
# the patched BlueZ (manual) only ever warn.
OWNED_CHECKS = frozenset({
    "hostname", "steam_library", "sshd",
    "node_exporter_version", "solar_metrics_script", "solar_metrics_timer",
    "vector_version", "vector_wrapper", "vector_active",
})
OWNED_PREFIXES = ("/etc/", "home/")
VR_CHECKS = frozenset({
    "steamvr_linux_fixes", "ignition_shim", "psvr2toolkit_dll", "openvr_driver",
    "compositor_cap_sys_nice", "vrmonitor_handler", "openxr_runtime", "steamvr_settings",
})

STEAM_APPS = ".local/share/Steam/steamapps/common"
STEAMVR = f"{STEAM_APPS}/SteamVR"
PLUGIN = f"{STEAM_APPS}/PlayStation VR2 App/SteamVR_Plug-In"
STEAMVR_SETTINGS = ".steam/steam/config/steamvr.vrsettings"
SONY_DLL = "driver_playstation_vr2.dll"
SONY_DLL_KEPT = "driver_playstation_vr2_orig.dll"
# Every PSVR2Toolkit build of the dll carries it; Sony's never does.
TOOLKIT_MARKER = b"PlayStation VR2 Toolkit"
IGNITION_SUPPORT = (
    "proton", "launch_serverhelper.sh", "driver_install.sh", "driver_uninstall.sh",
    "wine_hidraw.reg",
)
SCHEME_HANDLERS = (
    ("x-scheme-handler/vrmonitor", "valve-vrmonitor.desktop"),
    ("x-scheme-handler/steamvr", "valve-steamvr.desktop"),
)


def capture(*cmd: str) -> str:
    return _run(cmd, check=True, capture_output=True, text=True).stdout


def succeeds(*cmd: str) -> bool:
    return _run(cmd, check=False, capture_output=True).returncode == 0


# --- Bazzite layer (rpm-ostree, SELinux) -------------------------------------------


def bazzite_missing_kargs(wanted: list[str]) -> list[str]:
    current = capture("rpm-ostree", "kargs").split()
    return [karg for karg in wanted if karg not in current]


def bazzite_append_karg(karg: str) -> None:
    _run(["rpm-ostree", "kargs", f"--append-if-missing={karg}"], check=True)


def bazzite_relabel(path: str) -> None:
    _run(["restorecon", path], check=True)


# --- desktop session ---------------------------------------------------------------


def session_env(uid: int) -> dict[str, str] | None:
    runtime = f"/run/user/{uid}"
    if not Path(runtime, "bus").exists():
        return None
    return {"XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}


def as_user(user: str, session: dict[str, str] | None, *cmd: str) -> None:
    exports = [f"{key}={value}" for key, value in (session or {}).items()]
    _run(["runuser", "-u", user, "--", "env", *exports, *cmd], check=True)


# --- base --------------------------------------------------------------------------


def ensure_hostname(name: str) -> None:
    if capture("hostnamectl", "--static").strip() == name:
        log.sub(f"hostname already {name}")
        return
    _run(["hostnamectl", "hostname", name], check=True)
    log.ok(f"hostname set to {name}")


def ensure_kargs(ctx: InstallContext) -> None:
    missing = bazzite_missing_kargs(env.text(ctx, "KARGS", "").split())
    for karg in missing:
        bazzite_append_karg(karg)
        log.ok(f"kernel argument {karg} added")
    if missing:
        log.warn("reboot required: kernel arguments apply on the next boot (never done here)")
    else:
        log.sub("kernel arguments already set")


def install_etc(ctx: InstallContext) -> None:
    if files.install_validated(ctx, SSHD_DROPIN, ["sshd", "-t"]):
        _run(["systemctl", "reload", "sshd"], check=True)
    systemd.ensure_running(ctx, "sshd.service", False)
    for name in ETC_OTHER:
        files.install(ctx, name)
    if any([files.install(ctx, name) for name in UDEV_RULES]):
        _run(["udevadm", "control", "--reload"], check=True)


def ensure_steam_library(user: str) -> None:
    fstab = Path("/etc/fstab")
    if not any(line.startswith("LABEL=steam ") for line in fstab.read_text().splitlines()):
        if not Path("/dev/disk/by-label/steam").exists():
            log.warn("no ext4 labelled 'steam' (scsi1): format it first, see the Obsidian guide")
            return
        with fstab.open("a") as handle:
            handle.write(STEAM_FSTAB + "\n")
        Path(STEAM_MOUNT).mkdir(parents=True, exist_ok=True)
        _run(["systemctl", "daemon-reload"], check=True)
        _run(["mount", STEAM_MOUNT], check=True)
        log.ok(f"{STEAM_MOUNT} added to fstab and mounted")
    if os.path.ismount(STEAM_MOUNT) and Path(STEAM_MOUNT).owner() != user:
        account = pwd.getpwnam(user)
        os.chown(STEAM_MOUNT, account.pw_uid, account.pw_gid)
        log.ok(f"{STEAM_MOUNT} owned by {user}")


def make_user_dirs(path: Path, home: Path, uid: int, gid: int) -> None:
    """Create `path` and any missing parents under `home`, owned by the user."""
    missing = []
    for parent in (path, *path.parents):
        if parent == home or parent.exists():
            break
        missing.append(parent)
    for directory in reversed(missing):
        directory.mkdir()
        os.chown(directory, uid, gid)


def install_home(ctx: InstallContext, user: str, home: str) -> bool:
    account = pwd.getpwnam(user)
    if account.pw_dir != home:
        raise InstallError(f"{user}'s home is {account.pw_dir}, the build expected {home}")
    changed = False
    for name, (dest, _mode) in ctx.file_map.items():
        if not name.startswith("home/"):
            continue
        make_user_dirs(Path(dest).parent, Path(home), account.pw_uid, account.pw_gid)
        changed |= files.install(ctx, name)
        os.chown(dest, account.pw_uid, account.pw_gid)
    return changed


def configure_session(user: str, units_changed: bool) -> None:
    session = session_env(pwd.getpwnam(user).pw_uid)
    if session is None:
        log.warn(f"{user} has no session bus (not logged in): skipped user units and KDE idle")
        return
    if units_changed:
        as_user(user, session, "systemctl", "--user", "daemon-reload")
    for file, groups, key, value in KDE_SETTINGS:
        group_args = [arg for group in groups for arg in ("--group", group)]
        as_user(user, session, "kwriteconfig6", "--notify", "--file", file,
                *group_args, "--key", key, value)
    log.sub("KDE idle dim, screen-off and lock disabled")


def base(ctx: InstallContext, user: str, home: str) -> None:
    log.action("Hostname")
    ensure_hostname(env.text(ctx, "HOSTNAME", ctx.host))
    log.action("Kernel arguments")
    ensure_kargs(ctx)
    log.action("/etc: key-only sshd, auto-login, udev")
    install_etc(ctx)
    log.action("Steam library disk")
    ensure_steam_library(user)
    log.action("Headset-on SteamVR session and KDE")
    configure_session(user, install_home(ctx, user, home))


# --- metrics -----------------------------------------------------------------------


def node_exporter_version() -> str:
    result = _run([NODE_EXPORTER, "--version"], check=False, capture_output=True, text=True)
    first = (result.stdout or result.stderr or "").splitlines()[:1]
    words = first[0].split() if first else []
    return words[2] if len(words) > 2 else ""


def fetch(url: str, digest: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / url.rsplit("/", 1)[1]
    if not target.is_file():
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - pinned https URL
            target.write_bytes(response.read())
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        target.unlink()
        raise InstallError(f"{url}: sha256 mismatch (download removed)")
    return target


def ensure_node_exporter(ctx: InstallContext) -> bool:
    version = env.text(ctx, "NODE_EXPORTER_VERSION", "")
    if node_exporter_version() == version and not ctx.force_update:
        log.sub(f"node_exporter {version} already installed")
        return False
    archive = fetch(env.text(ctx, "NODE_EXPORTER_URL", ""), env.text(ctx, "NODE_EXPORTER_SHA", ""))
    member = f"node_exporter-{version}.linux-amd64/node_exporter"
    with tarfile.open(archive) as tar:
        binary = tar.extractfile(member)
        if binary is None:
            raise InstallError(f"{archive.name} has no {member}")
        Path(NODE_EXPORTER).write_bytes(binary.read())
    os.chmod(NODE_EXPORTER, 0o755)
    bazzite_relabel(NODE_EXPORTER)
    log.ok(f"node_exporter {version} installed")
    return True


def metrics(ctx: InstallContext, user: str) -> None:
    log.action("node_exporter and solar-metrics")
    binary_changed = ensure_node_exporter(ctx)
    for name, (dest, _mode) in ctx.file_map.items():
        if name.startswith("libexec/") or name == "check.json":
            if files.install(ctx, name):
                bazzite_relabel(dest)
    account = pwd.getpwnam(user)
    textfile = Path(TEXTFILE_DIR)
    textfile.mkdir(parents=True, exist_ok=True)
    os.chown(textfile, account.pw_uid, account.pw_gid)
    changed = {Path(name).name for name in SYSTEM_UNITS if files.install(ctx, name)}
    if changed:
        systemd.daemon_reload(ctx)
    systemd.ensure_running(ctx, "node_exporter.service",
                           binary_changed or "node_exporter.service" in changed)
    systemd.ensure_running(ctx, "solar-metrics.timer", "solar-metrics.timer" in changed)
    systemd.run_once(ctx, "solar-metrics.service")


# --- vector ------------------------------------------------------------------------


def vector_version() -> str:
    """`vector 0.61.0 (x86_64-unknown-linux-gnu ...)` -> `0.61.0`."""
    try:
        result = _run([VECTOR, "--version"], check=False, capture_output=True, text=True)
    except OSError:
        return ""
    words = (result.stdout or "").split()
    return words[1] if len(words) > 1 and words[0] == "vector" else ""


def ensure_vector_binary(ctx: InstallContext) -> bool:
    version = env.text(ctx, "VECTOR_VERSION", "")
    if vector_version() == version and not ctx.force_update:
        log.sub(f"vector {version} already installed")
        return False
    archive = fetch(env.text(ctx, "VECTOR_URL", ""), env.text(ctx, "VECTOR_SHA", ""))
    with tarfile.open(archive) as tar:
        names = {name.removeprefix("./"): name for name in tar.getnames()}
        binary = tar.extractfile(names[VECTOR_MEMBER]) if VECTOR_MEMBER in names else None
        if binary is None:
            raise InstallError(f"{archive.name} has no {VECTOR_MEMBER}")
        staged = Path(f"{VECTOR}.vr-guest")
        staged.write_bytes(binary.read())
    os.chmod(staged, 0o755)
    os.replace(staged, VECTOR)
    bazzite_relabel(VECTOR)
    log.ok(f"vector {version} installed")
    return True


def install_vector(ctx: InstallContext) -> None:
    log.action("Vector: the journal to VictoriaLogs")
    env.require(ctx, "VECTOR_VERSION", "VECTOR_URL", "VECTOR_SHA",
                "VECTOR_QUERY_URL", "VECTOR_TENANT")
    changed = ensure_vector_binary(ctx)
    # Before the check: the config names it as journalctl_path.
    if files.install(ctx, VECTOR_WRAPPER):
        bazzite_relabel(ctx.file_map[VECTOR_WRAPPER][0])
        changed = True
    vector.check_config(VECTOR, ctx.build_dir / VECTOR_CONFIG, ctx.build_dir / VECTOR_TESTS)
    # Each its own call, never short-circuited.
    changed = files.install(ctx, VECTOR_CONFIG) or changed
    changed = files.install(ctx, VECTOR_UNIT_FILE) or changed
    starting = changed or not succeeds("systemctl", "is-active", "--quiet", VECTOR_UNIT)
    # ensure_running daemon-reloads before the restart when anything changed.
    systemd.ensure_running(ctx, VECTOR_UNIT, changed)
    systemd.require_active(ctx, VECTOR_UNIT)
    if starting:
        vector.confirm_delivery(env.text(ctx, "VECTOR_QUERY_URL", ""),
                                env.text(ctx, "VECTOR_TENANT", ""), VECTOR_MARKER_TAG, VECTOR_UNIT)


# --- vr ----------------------------------------------------------------------------


class Desktop(NamedTuple):
    name: str
    home: Path
    uid: int
    gid: int

    @classmethod
    def lookup(cls, name: str) -> Desktop:
        account = pwd.getpwnam(name)
        return cls(name, Path(account.pw_dir), account.pw_uid, account.pw_gid)


def steamvr_running() -> bool:
    return succeeds("pgrep", "-x", "vrserver")


def user_output(user: Desktop, *cmd: str) -> str:
    result = _run(["runuser", "-u", user.name, "--", *cmd],
                  check=False, capture_output=True, text=True)
    return result.stdout.strip()


def put(user: Desktop, dest: Path, data: bytes, mode: int) -> bool:
    """Write `data` as the user's file when it differs; True if it changed."""
    changed = dest.is_symlink() or not dest.is_file() or dest.read_bytes() != data
    if changed:
        make_user_dirs(dest.parent, user.home, user.uid, user.gid)
        staged = dest.with_name(f".{dest.name}.vr-guest")
        staged.write_bytes(data)
        os.replace(staged, dest)
    os.chmod(dest, mode)
    os.chown(dest, user.uid, user.gid)
    return changed


def ensure_link(user: Desktop, link: Path, target: Path) -> bool:
    if link.is_symlink() and os.readlink(link) == str(target):
        return False
    make_user_dirs(link.parent, user.home, user.uid, user.gid)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    os.lchown(link, user.uid, user.gid)
    return True


def same_member(path: Path, info: zipfile.ZipInfo) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != info.file_size:
        return False
    return zlib.crc32(path.read_bytes()) == info.CRC


def extract(user: Desktop, archive: Path, dest: Path, executable: frozenset[str] = frozenset(),
            ) -> int:
    """Unpack `archive` into `dest` as the user's files; returns how many changed.

    Members are compared by size and CRC first, so an unchanged 170 MB release is
    not rewritten on every deploy. Zip modes are not trusted (one release marks
    everything 0666, another every dll executable): `*.sh` and `executable` get 755.
    """
    changed = 0
    with zipfile.ZipFile(archive) as handle:
        for info in handle.infolist():
            if info.is_dir():
                continue
            if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                raise InstallError(f"{archive.name}: unsafe member {info.filename}")
            path = dest / info.filename
            name = Path(info.filename).name
            mode = 0o755 if name.endswith(".sh") or name in executable else 0o644
            data = None if same_member(path, info) else handle.read(info)
            if data is not None:
                changed += put(user, path, data, mode)
            else:
                os.chmod(path, mode)
                os.chown(path, user.uid, user.gid)
    return changed


def fetch_release(ctx: InstallContext, prefix: str) -> Path:
    return fetch(env.text(ctx, f"{prefix}_URL", ""), env.text(ctx, f"{prefix}_SHA", ""))


def report(what: str, changed: int | bool) -> None:
    if changed:
        log.ok(f"{what} updated")
    else:
        log.sub(f"{what} unchanged")


def install_svlf(ctx: InstallContext, user: Desktop) -> None:
    """SteamVRLinuxFixes, the Vulkan layer upstream's install.sh puts under ~/.local."""
    targets = {
        "libsteamvr_linux_fixes.so": (".local/lib", 0o755),
        "VkLayer_steamvr_linux_fixes.json": (".local/share/vulkan/implicit_layer.d", 0o644),
    }
    changed = False
    with zipfile.ZipFile(fetch_release(ctx, "SVLF")) as handle:
        for name, (directory, mode) in targets.items():
            changed |= put(user, user.home / directory / name, handle.read(name), mode)
    report("SteamVRLinuxFixes layer", changed)


def ignition_config(ignition: Path, driver: str) -> bytes:
    """The `ignition.json` upstream's install_ignition.sh writes."""
    config = {
        "server_exe": str(ignition / "ignition_server.exe"),
        "driver_dll": f"../win64/driver_{driver}.dll",
        "wine_cmd": ["./launch_serverhelper.sh"],
        "wait_for_debugger": False,
    }
    return (json.dumps(config, indent=4) + "\n").encode()


def install_ignition(ctx: InstallContext, user: Desktop, plugin: Path) -> None:
    """Ignition into ~/ignition, then what install_ignition.sh does to the plugin."""
    ignition = user.home / "ignition"
    changed = extract(user, fetch_release(ctx, "IGNITION"), ignition, frozenset({"proton"}))
    manifest = json.loads((plugin / "driver.vrdrivermanifest").read_text(encoding="utf-8"))
    driver = manifest["name"]
    linux64 = plugin / "bin" / "linux64"
    changed += put(user, linux64 / "ignition.json", ignition_config(ignition, driver), 0o644)
    for name in IGNITION_SUPPORT:
        mode = 0o755 if name == "proton" or name.endswith(".sh") else 0o644
        changed += put(user, linux64 / name, (ignition / name).read_bytes(), mode)
    changed += ensure_link(user, linux64 / f"driver_{driver}.so",
                           ignition / "libdriver_ignition.so")
    report("Ignition shim", changed)


def driver_registered(user: Desktop, plugin: Path) -> bool:
    try:
        paths = json.loads((user.home / ".config/openvr/openvrpaths.vrpath").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(paths, dict):
        return False
    drivers = paths.get("external_drivers") or []
    return any(os.path.realpath(driver) == os.path.realpath(plugin) for driver in drivers)


def register_driver(user: Desktop, steamvr: Path, plugin: Path) -> None:
    """What Ignition's driver_install.sh does, only when the driver is not registered."""
    if driver_registered(user, plugin):
        log.sub("PS VR2 driver already registered with SteamVR")
        return
    as_user(user.name, None, str(steamvr / "bin" / "vrpathreg.sh"), "adddriver", str(plugin))
    log.ok("PS VR2 driver registered with SteamVR")


def install_psvr2toolkit(ctx: InstallContext, user: Desktop, plugin: Path, dll_sha: str) -> None:
    """PSVR2Toolkit's dll in place of Sony's, which it loads as `_orig`.

    A PS VR2 App update or file verify puts Sony's dll back, possibly a newer one, so
    whatever non-toolkit dll is found becomes the kept original.
    """
    win64 = plugin / "bin" / "win64"
    dll, kept = win64 / SONY_DLL, win64 / SONY_DLL_KEPT
    current = dll.read_bytes() if dll.is_file() else None
    if (current is not None and hashlib.sha256(current).hexdigest() != dll_sha
            and TOOLKIT_MARKER not in current):
        os.replace(dll, kept)
        log.ok(f"Sony's driver kept as {SONY_DLL_KEPT}")
    if not kept.is_file():
        raise InstallError(f"no Sony driver in {win64}: verify the PS VR2 App's files in Steam")
    report("PSVR2Toolkit", extract(user, fetch_release(ctx, "PSVR2TK"), win64))


def install_unitysetup(ctx: InstallContext, user: Desktop) -> None:
    changed = extract(user, fetch_release(ctx, "UNITYSETUP"), user.home / "psvr2-unitysetup",
                      frozenset({"PSVR2Toolkit.UnitySetup.x86_64"}))
    report("PSVR2Toolkit.UnitySetup (play area tool)", changed)


def ensure_cap_sys_nice(steamvr: Path) -> None:
    """A SteamVR update drops it from the compositor launcher (missed frames)."""
    launcher = str(steamvr / "bin" / "linux64" / "vrcompositor-launcher")
    if "cap_sys_nice" in capture("getcap", launcher):
        log.sub("vrcompositor-launcher already has cap_sys_nice")
        return
    _run(["setcap", "CAP_SYS_NICE=eip", launcher], check=True)
    log.ok("vrcompositor-launcher cap_sys_nice set")


def ensure_scheme_handlers(user: Desktop) -> bool:
    """vrmonitor:// and steamvr://; False when SteamVR has not written its .desktop files."""
    applications = user.home / ".local/share/applications"
    if not (applications / "valve-vrmonitor.desktop").is_file():
        log.warn("valve-*.desktop missing: start SteamVR once, then deploy again")
        return False
    missing = [(scheme, desktop) for scheme, desktop in SCHEME_HANDLERS
               if user_output(user, "xdg-mime", "query", "default", scheme) != desktop]
    if not missing:
        log.sub("vrmonitor:// and steamvr:// handlers already set")
        return True
    as_user(user.name, None, "update-desktop-database", str(applications))
    for scheme, desktop in missing:
        as_user(user.name, None, "xdg-mime", "default", desktop, scheme)
    log.ok("vrmonitor:// and steamvr:// handlers set")
    return True


def ensure_steamvr_settings(user: Desktop, wanted: dict[str, object]) -> bool:
    """Merge `wanted` into the "steamvr" section; False if SteamVR started meanwhile."""
    path = user.home / STEAMVR_SETTINGS
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError as exc:
        raise InstallError(f"{path} is not valid JSON, left alone: {exc}") from exc
    section = settings.setdefault("steamvr", {})
    if all(section.get(key) == value for key, value in wanted.items()):
        log.sub("steamvr.vrsettings already set")
        return True
    # SteamVR rewrites the file on exit, so a change made under it is lost.
    if steamvr_running():
        log.warn("SteamVR started during the deploy: steamvr.vrsettings left alone")
        return False
    if path.is_file():
        files.back_up(str(path))
    section.update(wanted)
    put(user, path, (json.dumps(settings, indent=3) + "\n").encode(), 0o644)
    log.ok("steamvr.vrsettings updated")
    return True


def vr(ctx: InstallContext, user: Desktop) -> frozenset[str]:
    """The check ids the vr stage converged; empty when it was skipped."""
    log.action("SteamVR: Linux fixes, Ignition, PSVR2Toolkit, handlers, settings")
    steamvr, plugin = user.home / STEAMVR, user.home / PLUGIN
    if not (steamvr / "bin" / "linux64" / "vrcompositor-launcher").is_file():
        log.warn("SteamVR (250820) is not installed: vr stage skipped")
        return frozenset()
    if not (plugin / "driver.vrdrivermanifest").is_file():
        log.warn("PlayStation VR2 App (2580190) is not installed: vr stage skipped")
        return frozenset()
    if steamvr_running():
        log.warn("SteamVR is running (someone may be in the headset): vr stage skipped; "
                 "deploy again after `psvr2-steamvr quit`")
        return frozenset()
    manifest = json.loads((ctx.build_dir / "check.json").read_text(encoding="utf-8"))
    install_svlf(ctx, user)
    install_ignition(ctx, user, plugin)
    register_driver(user, steamvr, plugin)
    install_psvr2toolkit(ctx, user, plugin, manifest["psvr2toolkit_dll_sha256"])
    install_unitysetup(ctx, user)
    ensure_cap_sys_nice(steamvr)
    ensure_link(user, user.home / ".config/openxr/1/active_runtime.json",
                steamvr / "steamxr_linux64.json")
    converged = set(VR_CHECKS)
    if not ensure_scheme_handlers(user):
        converged.discard("vrmonitor_handler")
    if not ensure_steamvr_settings(user, manifest["steamvr_settings"]):
        converged.discard("steamvr_settings")
    return frozenset(converged)


# --- verify ------------------------------------------------------------------------


def owned(check_id: str, extra: frozenset[str] = frozenset()) -> bool:
    return check_id in OWNED_CHECKS or check_id in extra or check_id.startswith(OWNED_PREFIXES)


def parse_check(output: str) -> list[tuple[str, str]]:
    """(id, reason) for every FAIL line of solar-check's output."""
    failures = []
    for line in output.splitlines():
        words = line.split(None, 1)
        if len(words) == 2 and words[0] == "FAIL":
            check_id, _, reason = words[1].partition(": ")
            failures.append((check_id, reason))
    return failures


def verify(ctx: InstallContext, user: str, extra: frozenset[str] = frozenset()) -> None:
    log.action("solar-check")
    check = env.text(ctx, "CHECK_PATH", "")
    result = _run(["runuser", "-u", user, "--", check], check=False, capture_output=True, text=True)
    print(result.stdout, end="")
    if result.returncode not in (0, 1):
        raise InstallError(f"{check} exited {result.returncode}: {result.stderr.strip()}")
    failures = parse_check(result.stdout)
    for check_id, reason in failures:
        if not owned(check_id, extra):
            log.warn(f"{check_id}: {reason}")
    broken = [check_id for check_id, _ in failures if owned(check_id, extra)]
    if broken:
        raise InstallError(f"solar-check failed after deploy: {', '.join(broken)}")
    log.ok("solar-check: everything this module owns passes")


def install(ctx: InstallContext) -> None:
    log.header("VR Guest")
    if env.text(ctx, "OS_TYPE", "") != "bazzite":
        raise InstallError(f"unsupported OS_TYPE {env.text(ctx, 'OS_TYPE', '')!r}")
    env.require(ctx, "DESKTOP_USER", "DESKTOP_HOME", "NODE_EXPORTER_VERSION", "CHECK_PATH")
    user = env.text(ctx, "DESKTOP_USER", "")
    base(ctx, user, env.text(ctx, "DESKTOP_HOME", ""))
    metrics(ctx, user)
    install_vector(ctx)
    verify(ctx, user, vr(ctx, Desktop.lookup(user)))


if __name__ == "__main__":
    run(install, "VR Guest")
