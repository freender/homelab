#!/usr/bin/env python3
"""Remote installer for the ubuntu-setup module (freender/homelab-ops#30).

Base OS setup for the Ubuntu bare-metal hosts: hostname and timezone, the
primary NIC name, Docker CE, deploy-user sudoers, sshd hardening, ZFS ARC and
inotify limits, and optionally WireGuard. Both targets are offsite, so a broken
sshd or NIC name is recovered through Pi-KVM, not a console.

Worth knowing before reading:

* **Every refusal happens before anything is written.** The env file, the
  staged file map, the timezone, the deploy user and the staged sudoers file are
  all checked first. The bash set the hostname and timezone before `visudo`
  ever looked at the sudoers file.
* **The effective sshd config is verified on every deploy**, not just the file
  written. sshd keeps the *first* value it reads for most keywords, and cloud-init
  writes `50-cloud-init.conf`, which sorts ahead of our `99-` drop-in. Both hosts
  carry one today (it says `PasswordAuthentication no`). If it is ever
  regenerated with `yes`, the bash would have reported a hardened host that
  accepts passwords.
* **Converged files are not rewritten.** The bash rewrote `/etc/timezone`, the
  `/etc/localtime` link and the udev rule on every deploy, so their mtimes only
  ever recorded the last deploy.
* **The udev rule is the rendered build file**, not a second heredoc copy of the
  template. A fallback NIC (preferred MAC not on the host) swaps only the MAC.
* **The NetworkManager check can now match.** `nmcli -t` escapes colons in a MAC
  (`AA\\:BB`), so the bash grep for `aa:bb` never did.
* **A failed `update-initramfs` names `--force` as the retry**, because a plain
  redeploy finds `zfs.conf` in place and skips it. Same trap `pve-gpu-passthrough`
  had.
* **The legacy `/var/lib/homelab/ubuntu-setup` bundle and
  `01-disable-password-auth.conf` cleanups are not ported.** Neither exists on
  either host; checked, not assumed.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

NIC_RULE = "10-network-names.rules"
SSHD_HARDENING = "sshd-hardening.conf"
REQUIRED_BUILD_FILES = ("sudoers", NIC_RULE, SSHD_HARDENING, "zfs.conf", "99-inotify.conf")
WIREGUARD_SYSCTL = "99-wireguard.conf"
UNWANTED_UNITS = {"openipmi.service": "no BMC/IPMI device"}
DOCKER_INSTALLER_URL = "https://get.docker.com"
# sshd -T prints the canonical keyword; the drop-in may use a deprecated alias.
SSHD_KEYWORD_ALIASES = {"challengeresponseauthentication": "kbdinteractiveauthentication"}

# Module-level so tests can rebind them under tmp_path.
ZONEINFO = "/usr/share/zoneinfo"
LOCALTIME = "/etc/localtime"
ETC_TIMEZONE = "/etc/timezone"
SYS_CLASS_NET = "/sys/class/net"
UDEV_RULES_DIR = "/etc/udev/rules.d"
LEGACY_PERSISTENT_NET = "/etc/udev/rules.d/70-persistent-net.rules"
NETPLAN_DIR = "/etc/netplan"
DOCKER_APT_SOURCES = (
    "/etc/apt/sources.list.d/docker.list",
    "/etc/apt/sources.list.d/docker.sources",
)
ZFS_ARC_MAX_PARAM = "/sys/module/zfs/parameters/zfs_arc_max"
WIREGUARD_DIR = "/etc/wireguard"

# Indirection points for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which


def _output(argv: list[str]) -> str:
    result = _run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise InstallError(f"{' '.join(argv)} failed (exit {result.returncode})")
    return (result.stdout or "").strip()


def _check(argv: list[str], retry_hint: str = "") -> None:
    result = _run(argv, check=False, stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        raise InstallError(f"{' '.join(argv)} failed (exit {result.returncode}){retry_hint}")


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(ctx: InstallContext) -> bool:
    """Every refusal, before any write. Returns whether WireGuard is enabled."""
    env.require(
        ctx,
        "DEPLOY_USER",
        "PRIMARY_INTERFACE_NAME",
        "SYSTEM_HOSTNAME",
        "SYSTEM_TIMEZONE",
        "WIREGUARD_ENABLED",
        "ZFS_ARC_MAX",
    )
    # May be empty (no MAC secret: pin by fallback), but never absent.
    if "PRIMARY_INTERFACE_MAC" not in ctx.env:
        raise InstallError(
            f"incomplete env file at {ctx.build_dir / 'env'} (missing: PRIMARY_INTERFACE_MAC)"
        )
    if not ctx.env["ZFS_ARC_MAX"].isdigit():
        raise InstallError(f"ZFS_ARC_MAX must be a byte count, got {ctx.env['ZFS_ARC_MAX']!r}")

    wireguard = env.flag(ctx, "WIREGUARD_ENABLED")
    needed = (*REQUIRED_BUILD_FILES, *((WIREGUARD_SYSCTL,) if wireguard else ()))
    missing = [
        name for name in needed if name not in ctx.file_map or not (ctx.build_dir / name).is_file()
    ]
    if missing:
        raise InstallError(f"staged bundle is incomplete (missing: {', '.join(missing)})")

    timezone = ctx.env["SYSTEM_TIMEZONE"]
    if not (Path(ZONEINFO) / timezone).is_file():
        raise InstallError(f"timezone data not found for {timezone}")

    try:
        pwd.getpwnam(ctx.env["DEPLOY_USER"])
    except KeyError as exc:
        raise InstallError(f"deploy user {ctx.env['DEPLOY_USER']} does not exist") from exc

    # An invalid file in /etc/sudoers.d breaks sudo host-wide the instant it lands.
    if _run(
        ["visudo", "-cf", str(ctx.build_dir / "sudoers")], check=False, stdout=subprocess.DEVNULL
    ).returncode:
        raise InstallError("staged sudoers file is invalid; refusing to install")
    return wireguard


# ---------------------------------------------------------------------------
# Hostname and timezone
# ---------------------------------------------------------------------------


def ensure_hostname(hostname: str) -> None:
    if _output(["hostnamectl", "status", "--static"]) == hostname:
        log.sub(f"Hostname already set to {hostname}")
        return
    _check(["hostnamectl", "set-hostname", hostname])
    log.ok(f"Hostname set to {hostname}")


def ensure_timezone(timezone: str) -> None:
    if _output(["timedatectl", "show", "--property=Timezone", "--value"]) == timezone:
        log.sub(f"Timezone already set to {timezone}")
    else:
        _check(["timedatectl", "set-timezone", timezone])
        log.ok(f"Timezone set to {timezone}")
    sync_timezone_files(timezone)


def sync_timezone_files(timezone: str) -> None:
    """Keep `/etc/localtime` and `/etc/timezone` consistent with the timezone.

    `timedatectl` maintains `/etc/localtime` itself; this is the same
    belt-and-braces `pve-postinstall` does, for a host where it silently no-ops.
    The link is replaced atomically, so no reader ever sees it missing.
    """
    target = str(Path(ZONEINFO) / timezone)
    localtime = Path(LOCALTIME)
    if not (localtime.is_symlink() and os.readlink(localtime) == target):
        staged = localtime.with_name(f".{localtime.name}.homelab")
        staged.unlink(missing_ok=True)
        staged.symlink_to(target)
        os.replace(staged, localtime)
        log.sub(f"Linked {LOCALTIME} to {target}")

    etc_timezone = Path(ETC_TIMEZONE)
    wanted = f"{timezone}\n"
    if not (etc_timezone.is_file() and etc_timezone.read_text(encoding="utf-8") == wanted):
        etc_timezone.write_text(wanted, encoding="utf-8")
        log.sub(f"Wrote {ETC_TIMEZONE}")


# ---------------------------------------------------------------------------
# Primary NIC pinning
# ---------------------------------------------------------------------------


def _read_sys(iface: str, attribute: str) -> str:
    """A sysfs attribute, or "" -- `carrier` raises EINVAL on a downed link."""
    try:
        return (Path(SYS_CLASS_NET) / iface / attribute).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _usable(iface: str) -> bool:
    return _read_sys(iface, "type") == "1" and _read_sys(iface, "carrier") == "1"


def default_route_iface() -> str:
    """The `dev` of the first default route. The bash took awk's `$5`, which is
    `link` rather than the device for a `default dev wg0 scope link` route."""
    words = _output(["ip", "route", "show", "default"]).split("\n", 1)[0].split()
    return words[words.index("dev") + 1] if "dev" in words[:-1] else ""


def fallback_iface() -> str:
    """The default-route interface if it is a live ethernet link, else the first
    live ethernet link by name, else ""."""
    preferred = default_route_iface()
    if preferred and _usable(preferred):
        return preferred
    for path in sorted(Path(SYS_CLASS_NET).iterdir()):
        if path.name != "lo" and _usable(path.name):
            return path.name
    return ""


def select_mac(preferred: str) -> str:
    """The preferred MAC if some interface has it, else the fallback link's MAC."""
    present = {_read_sys(path.name, "address").lower() for path in Path(SYS_CLASS_NET).iterdir()}
    if preferred and preferred.lower() in present:
        return preferred
    iface = fallback_iface()
    if not iface:
        return ""
    log.warn(f"primary MAC {preferred or '(unset)'} not on this host; pinning {iface} instead")
    return _read_sys(iface, "address")


def warn_competing_nic_rules(ctx: InstallContext, dest: str, mac: str) -> None:
    """Warn about other mechanisms that could name the same NIC. Never fails a deploy."""
    mac = mac.lower()
    if Path(LEGACY_PERSISTENT_NET).is_file():
        log.warn(f"legacy {LEGACY_PERSISTENT_NET} present; may race with {dest}")

    # Same line, and NAME as a whole key: `ENV{ID_NET_NAME}=` is not a rename.
    rename = re.compile(r"\bNAME\s*:?=")
    for rule in sorted(Path(UDEV_RULES_DIR).glob("*.rules")):
        if rule.name == Path(dest).name:
            continue
        lines = rule.read_text(encoding="utf-8", errors="replace").lower().splitlines()
        if any(mac in line and rename.search(line.upper()) for line in lines):
            log.warn(f"competing udev naming rule for {mac} in {rule}")

    for plan in sorted(Path(NETPLAN_DIR).glob("*.yaml")):
        if mac in plan.read_text(encoding="utf-8", errors="replace").lower():
            log.warn(
                f"netplan config {plan} also references {mac}; verify it does not set a "
                "conflicting name (cloud-init can regenerate this file on next boot)"
            )

    if (
        _which("nmcli")
        and _run(["systemctl", "is-active", "--quiet", "NetworkManager"], check=False).returncode
        == 0
    ):
        result = _run(
            ["nmcli", "-t", "-f", "GENERAL.HWADDR", "device", "show"],
            check=False,
            capture_output=True,
            text=True,
        )
        if mac in (result.stdout or "").replace("\\", "").lower():
            log.warn(
                f"NetworkManager is active and manages a device with MAC {mac}; it may "
                "rename/reconfigure this interface independently of udev"
            )


def pin_primary_nic(ctx: InstallContext) -> None:
    name, preferred = ctx.env["PRIMARY_INTERFACE_NAME"], ctx.env["PRIMARY_INTERFACE_MAC"]
    dest, mode = ctx.file_map[NIC_RULE]
    mac = select_mac(preferred)
    if not mac:
        log.warn("Could not determine a primary ethernet interface to pin")
        return
    warn_competing_nic_rules(ctx, dest, mac)

    src = ctx.build_dir / NIC_RULE
    if mac != preferred:
        rendered = src.read_text(encoding="utf-8")
        match = f'ATTR{{address}}=="{preferred}"'
        if rendered.count(match) != 1:
            raise InstallError(
                f"{src} does not carry exactly one {match}; template and installer disagree"
            )
        src = ctx.build_dir / f"{NIC_RULE}.fallback"
        src.write_text(rendered.replace(match, f'ATTR{{address}}=="{mac}"'), encoding="utf-8")

    dest_path = Path(dest)
    before = dest_path.read_bytes() if dest_path.is_file() else None
    files.install_from(ctx, src, dest, mode, record=NIC_RULE)
    log.ok(f"Pinned primary interface as {name}")
    # By content, not by install_from's return: that is True on every --force deploy.
    if dest_path.read_bytes() != before:
        log.warn(
            "Primary NIC pinning changed; reboot may be required before "
            "interface-name-dependent services are reliable"
        )


# ---------------------------------------------------------------------------
# Docker CE
# ---------------------------------------------------------------------------


def remove_snap_docker() -> None:
    if (
        not _which("snap")
        or _run(["snap", "list", "docker"], check=False, capture_output=True).returncode
    ):
        return
    log.sub("Removing snap docker before installing Docker CE...")
    _run(["snap", "stop", "docker"], check=False, capture_output=True)
    _run(["snap", "disable", "docker"], check=False, capture_output=True)
    _check(["snap", "remove", "--purge", "docker"])
    log.ok("Removed snap docker")


def ensure_docker(ctx: InstallContext) -> None:
    remove_snap_docker()
    if not _which("docker"):
        reason, args, done = "Docker CE not installed", [], "Docker CE installed"
    elif not any(Path(source).is_file() for source in DOCKER_APT_SOURCES):
        reason, args, done = (
            "Docker apt source missing",
            ["--setup-repo"],
            "Docker apt source configured",
        )
    else:
        log.sub("Docker already installed")
        return

    log.sub(f"{reason}; running Docker convenience installer...")
    packages.ensure(ctx, "ca-certificates", "curl")
    with tempfile.TemporaryDirectory(prefix="get-docker.") as tmp:
        script = str(Path(tmp) / "get-docker.sh")
        _check(["curl", "-fsSL", DOCKER_INSTALLER_URL, "-o", script])
        _check(["sh", script, *args])
    log.ok(done)


def ensure_docker_group(user: str) -> None:
    try:
        group = grp.getgrnam("docker")
    except KeyError as exc:
        raise InstallError("docker group does not exist after installing Docker CE") from exc
    if user in group.gr_mem or pwd.getpwnam(user).pw_gid == group.gr_gid:
        log.sub(f"{user} already in docker group")
        return
    _check(["usermod", "-aG", "docker", user])
    log.ok(f"Added {user} to docker group")


# ---------------------------------------------------------------------------
# sshd
# ---------------------------------------------------------------------------


def expected_sshd_settings(path: Path) -> dict[str, str]:
    """`keyword value` pairs from the hardening drop-in, keyed as `sshd -T` prints them."""
    settings: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if len(words) >= 2 and not words[0].startswith("#"):
            key = words[0].lower()
            settings[SSHD_KEYWORD_ALIASES.get(key, key)] = " ".join(words[1:]).lower()
    return settings


def verify_effective_sshd(ctx: InstallContext) -> None:
    """Fail unless the running config really says what the drop-in says.

    The file being on disk proves nothing: sshd keeps the first value it reads,
    so any earlier `sshd_config.d` file or `sshd_config` line wins over ours.
    """
    effective: dict[str, str] = {}
    for line in _output(["sshd", "-T"]).splitlines():
        key, _, value = line.partition(" ")
        effective[key.lower()] = value.strip().lower()

    dest = ctx.file_map[SSHD_HARDENING][0]
    wrong = [
        f"{key} is {effective.get(key, '(unset)')}, expected {value}"
        for key, value in expected_sshd_settings(ctx.build_dir / SSHD_HARDENING).items()
        if effective.get(key) != value
    ]
    if wrong:
        raise InstallError(
            f"effective sshd config overrides {dest}: {'; '.join(wrong)}. sshd keeps the first "
            "value it reads, so an earlier file in /etc/ssh/sshd_config.d or sshd_config wins"
        )
    log.ok("Effective sshd config matches the hardening drop-in")


def harden_sshd(ctx: InstallContext) -> None:
    if files.install_validated(ctx, SSHD_HARDENING, ["sshd", "-t"]):
        _check(["systemctl", "reload", "ssh"])
        log.ok("SSH reloaded")
    verify_effective_sshd(ctx)


# ---------------------------------------------------------------------------
# Kernel tunables and WireGuard
# ---------------------------------------------------------------------------


def apply_zfs_arc(ctx: InstallContext) -> None:
    if not files.install(ctx, "zfs.conf"):
        return
    _check(
        ["update-initramfs", "-u"],
        retry_hint=". zfs.conf is already in place, so a plain redeploy will skip this step; "
        "re-run the deploy with --force",
    )
    try:
        Path(ZFS_ARC_MAX_PARAM).write_text(f"{ctx.env['ZFS_ARC_MAX']}\n", encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"could not set {ZFS_ARC_MAX_PARAM}: {exc}") from exc
    log.ok("ZFS ARC limit applied")


def apply_sysctl(ctx: InstallContext, name: str, label: str) -> None:
    if files.install(ctx, name):
        _check(["sysctl", "--system"])
        log.ok(f"{label} applied")


def ensure_wireguard(ctx: InstallContext) -> None:
    log.action("WireGuard packages")
    packages.ensure(ctx, "wireguard", "wireguard-tools")

    log.action("WireGuard sysctl")
    apply_sysctl(ctx, WIREGUARD_SYSCTL, "WireGuard sysctl")

    log.action("WireGuard services")
    for conf in sorted(Path(WIREGUARD_DIR).glob("*.conf")):
        systemd.ensure_running(ctx, f"wg-quick@{conf.stem}.service", changed=False)


def install(ctx: InstallContext) -> None:
    log.header("Ubuntu Setup")
    wireguard = preflight(ctx)

    log.action("Hostname and timezone")
    ensure_hostname(ctx.env["SYSTEM_HOSTNAME"])
    ensure_timezone(ctx.env["SYSTEM_TIMEZONE"])

    log.action("Unwanted default services")
    for unit, reason in UNWANTED_UNITS.items():
        systemd.mask(ctx, unit, reason)

    log.action("Primary NIC pinning")
    pin_primary_nic(ctx)

    log.action("Docker CE")
    ensure_docker(ctx)
    ensure_docker_group(ctx.env["DEPLOY_USER"])

    log.action("Sudoers")
    files.install(ctx, "sudoers")

    log.action("SSH hardening")
    harden_sshd(ctx)

    log.action("ZFS ARC limit")
    apply_zfs_arc(ctx)

    log.action("Inotify limits")
    apply_sysctl(ctx, "99-inotify.conf", "Inotify limits")

    if wireguard:
        ensure_wireguard(ctx)


if __name__ == "__main__":
    run(install, "Ubuntu Setup")
