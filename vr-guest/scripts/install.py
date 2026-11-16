#!/usr/bin/env python3
"""Remote installer for vr-guest (freender/homelab-ops#400; design in #399).

The `base` and `metrics` stages of the retired `setup.sh`, run as root. Steps that
need the desktop user's session (`systemctl --user`, `kwriteconfig6 --notify`) go
through `runuser` with that session's bus; with nobody logged in they are skipped
with a warning rather than failing the deploy.

Kernel-argument changes only take effect after a reboot, which this never does:
it reports "reboot required" and leaves the power-cycle to a human, because
someone may be in the headset.

Ends by running `solar-check` as the desktop user, the same check `solar-metrics`
publishes every 15 minutes. A failure this module owns fails the deploy; the
`vr` items (setup.sh `vr`, #401) and a not-yet-booted karg only warn.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from homelab_install import env, files, log, run, systemd
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
# this module's failure. Everything else (the vr stage, booted kargs) warns.
OWNED_CHECKS = frozenset({
    "hostname", "steam_library", "sshd",
    "node_exporter_version", "solar_metrics_script", "solar_metrics_timer",
})
OWNED_PREFIXES = ("/etc/", "home/")


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


# --- verify ------------------------------------------------------------------------


def owned(check_id: str) -> bool:
    return check_id in OWNED_CHECKS or check_id.startswith(OWNED_PREFIXES)


def parse_check(output: str) -> list[tuple[str, str]]:
    """(id, reason) for every FAIL line of solar-check's output."""
    failures = []
    for line in output.splitlines():
        words = line.split(None, 1)
        if len(words) == 2 and words[0] == "FAIL":
            check_id, _, reason = words[1].partition(": ")
            failures.append((check_id, reason))
    return failures


def verify(ctx: InstallContext, user: str) -> None:
    log.action("solar-check")
    check = env.text(ctx, "CHECK_PATH", "")
    result = _run(["runuser", "-u", user, "--", check], check=False, capture_output=True, text=True)
    print(result.stdout, end="")
    if result.returncode not in (0, 1):
        raise InstallError(f"{check} exited {result.returncode}: {result.stderr.strip()}")
    failures = parse_check(result.stdout)
    for check_id, reason in failures:
        if not owned(check_id):
            log.warn(f"{check_id}: {reason}")
    broken = [check_id for check_id, _ in failures if owned(check_id)]
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
    verify(ctx, user)


if __name__ == "__main__":
    run(install, "VR Guest")
