#!/usr/bin/env python3
"""Remote installer for the guest-baseline module (freender/homelab-ops#162).

The login and locale baseline of a Debian guest: the `freender` account, its
sudoers file, the key-only sshd drop-in, the timezone and the locale.

* **Every refusal happens before anything is written** -- a bad env file, a
  missing staged file, an invalid sudoers file, unknown timezone data, no
  `locale-gen`, or a uid/gid that another account already holds.
* **An existing account is never reshaped.** Its uid, shell, home and
  `authorized_keys` are its own; only `sudo` membership is added if missing.
  The staged `authorized_keys` is written only into an account this run created.
* **The effective sshd config is verified**, not just the file written
  (`homelab_install.sshd`). The drop-in it supersedes is removed only after the
  new one has passed `sshd -t`.
* **systemd-resolved's DNS cache is turned off where it runs** (homelab-ops#235),
  and the merged config it reads is verified to say `Cache=no`. A guest without
  it (the LXCs) gets no drop-in: nothing there caches.
* **`PLAN_ONLY=true` (a dry run) writes nothing.** Each step prints what it
  would change, and the sshd check reports the settings that would move.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path

from homelab_install import env, files, log, run, sshd, sudoers
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

SUDOERS = "sudoers"
SSHD_DROP_IN = "99-homelab-key-only.conf"
RESOLVED_DROP_IN = "99-homelab-no-dns-cache.conf"
AUTHORIZED_KEYS = "authorized_keys"
SUDO_GROUP = "sudo"
RESOLVED = "systemd-resolved"

# Module-level so tests can rebind them under tmp_path.
ZONEINFO = "/usr/share/zoneinfo"
LOCALE_GEN = "/etc/locale.gen"
DEFAULT_LOCALE = "/etc/default/locale"

# Indirection points for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which
_chown = os.chown


def _output(argv: list[str]) -> str:
    result = _run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise InstallError(f"{' '.join(argv)} failed (exit {result.returncode})")
    return (result.stdout or "").strip()


def _check(argv: list[str]) -> None:
    result = _run(argv, check=False, stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        raise InstallError(f"{' '.join(argv)} failed (exit {result.returncode})")


def _user(name: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(ctx: InstallContext) -> None:
    env.require(ctx, "USER_NAME", "USER_ID", "TIMEZONE", "LOCALE")
    if "RETIRED_SSHD_DROP_INS" not in ctx.env:
        raise InstallError(
            f"incomplete env file at {ctx.build_dir / 'env'} (missing: RETIRED_SSHD_DROP_INS)"
        )
    if not ctx.env["USER_ID"].isdigit():
        raise InstallError(f"USER_ID must be a number, got {ctx.env['USER_ID']!r}")

    missing = [
        name
        for name in (SUDOERS, SSHD_DROP_IN, RESOLVED_DROP_IN, AUTHORIZED_KEYS)
        if not (ctx.build_dir / name).is_file()
        or (name != AUTHORIZED_KEYS and name not in ctx.file_map)
    ]
    if missing:
        raise InstallError(f"staged bundle is incomplete (missing: {', '.join(missing)})")

    timezone = ctx.env["TIMEZONE"]
    if not (Path(ZONEINFO) / timezone).is_file():
        raise InstallError(f"timezone data not found for {timezone}")
    if not _which("locale-gen"):
        raise InstallError("locale-gen not found; install the locales package (base-packages)")

    if _user(ctx.env["USER_NAME"]) is None:
        check_ids_free(ctx.env["USER_NAME"], int(ctx.env["USER_ID"]))
    sudoers.check_staged(ctx.build_dir / SUDOERS)


def check_ids_free(name: str, uid: int) -> None:
    """A new account gets uid and gid `uid`; refuse if another account holds either."""
    try:
        holder = pwd.getpwuid(uid).pw_name
    except KeyError:
        pass
    else:
        raise InstallError(f"cannot create {name}: uid {uid} belongs to {holder}")
    try:
        group = grp.getgrgid(uid).gr_name
    except KeyError:
        group = name
    if group != name:
        raise InstallError(f"cannot create {name}: gid {uid} belongs to group {group}")
    try:
        gid = grp.getgrnam(name).gr_gid
    except KeyError:
        gid = uid
    if gid != uid:
        raise InstallError(f"cannot create {name}: group {name} already exists with gid {gid}")


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


def in_sudo_group(user: pwd.struct_passwd) -> bool:
    sudo = grp.getgrnam(SUDO_GROUP)
    return user.pw_gid == sudo.gr_gid or user.pw_name in sudo.gr_mem


def ensure_user(ctx: InstallContext, plan_only: bool) -> None:
    name, uid = ctx.env["USER_NAME"], ctx.env["USER_ID"]
    user = _user(name)
    if user is not None:
        if in_sudo_group(user):
            log.sub(f"{name} exists and is in {SUDO_GROUP}")
            return
        if plan_only:
            log.sub(f"Would add {name} to {SUDO_GROUP}")
            return
        _check(["usermod", "-aG", SUDO_GROUP, name])
        log.ok(f"Added {name} to {SUDO_GROUP}")
        return

    if plan_only:
        log.sub(f"Would create {name} (uid/gid {uid}, {SUDO_GROUP}, bash) with the homelab key")
        return
    try:
        grp.getgrnam(name)
    except KeyError:
        _check(["groupadd", "-g", uid, name])
    _check(["useradd", "-m", "-u", uid, "-g", uid, "-G", SUDO_GROUP, "-s", "/bin/bash", name])
    created = pwd.getpwnam(name)
    install_authorized_keys(ctx, created)
    log.ok(f"Created {name} (uid {uid})")


def install_authorized_keys(ctx: InstallContext, user: pwd.struct_passwd) -> None:
    ssh_dir = Path(user.pw_dir) / ".ssh"
    files.ensure_dir(ctx, str(ssh_dir), "700")
    files.install_to(ctx, AUTHORIZED_KEYS, str(ssh_dir / AUTHORIZED_KEYS), "600")
    for path in (ssh_dir, ssh_dir / AUTHORIZED_KEYS):
        _chown(path, user.pw_uid, user.pw_gid)


# ---------------------------------------------------------------------------
# Sudoers and sshd
# ---------------------------------------------------------------------------


def differs(ctx: InstallContext, name: str) -> bool:
    """Whether installing file-map entry `name` would change content or mode."""
    dest_str, mode = ctx.file_map[name]
    dest = Path(dest_str)
    if not dest.is_file():
        return True
    if not files.same_content(ctx.build_dir / name, dest):
        return True
    return dest.stat().st_mode & 0o7777 != int(mode, 8)


def ensure_sudoers(ctx: InstallContext, plan_only: bool) -> None:
    if not plan_only:
        files.install(ctx, SUDOERS)
    elif differs(ctx, SUDOERS):
        log.sub(f"Would update {ctx.file_map[SUDOERS][0]}")
    else:
        log.sub(f"{ctx.file_map[SUDOERS][0]} unchanged")


def retired_drop_ins(ctx: InstallContext) -> tuple[str, ...]:
    return tuple(ctx.env["RETIRED_SSHD_DROP_INS"].split())


def ensure_sshd(ctx: InstallContext, plan_only: bool) -> None:
    retired = retired_drop_ins(ctx)
    if not plan_only:
        sshd.harden(ctx, SSHD_DROP_IN, retired)
        return

    dest = ctx.file_map[SSHD_DROP_IN][0]
    if differs(ctx, SSHD_DROP_IN):
        log.sub(f"Would update {dest}, check it with sshd -t and reload ssh")
    for path in retired:
        if Path(path).exists():
            log.sub(f"Would remove {path} (superseded)")
    wrong = sshd.effective_differences(ctx.build_dir / SSHD_DROP_IN)
    if wrong:
        log.sub(f"Effective sshd settings that would change: {'; '.join(wrong)}")
    else:
        log.ok("Effective sshd config already matches the drop-in")


# ---------------------------------------------------------------------------
# Timezone and locale
# ---------------------------------------------------------------------------


def ensure_timezone(timezone: str, plan_only: bool) -> None:
    current = _output(["timedatectl", "show", "--property=Timezone", "--value"])
    if current == timezone:
        log.sub(f"Timezone already {timezone}")
    elif plan_only:
        log.sub(f"Would set timezone {current} -> {timezone}")
    else:
        _check(["timedatectl", "set-timezone", timezone])
        log.ok(f"Timezone set to {timezone}")


def locale_alias(locale: str) -> str:
    """How `locale -a` names a locale: `en_US.UTF-8` is listed as `en_US.utf8`."""
    base, _, codeset = locale.partition(".")
    return f"{base}.{codeset.lower().replace('-', '')}" if codeset else base


def enable_in_locale_gen(text: str, locale: str) -> str:
    """`text` with `<locale> <codeset>` uncommented, or appended if absent."""
    codeset = locale.partition(".")[2]
    entry = f"{locale} {codeset}"
    pattern = re.compile(rf"^#[ \t]*{re.escape(entry)}[ \t]*$", re.MULTILINE)
    if re.search(rf"^{re.escape(entry)}[ \t]*$", text, re.MULTILINE):
        return text
    if pattern.search(text):
        return pattern.sub(entry, text, count=1)
    return f"{text}{'' if not text or text.endswith(chr(10)) else chr(10)}{entry}\n"


def default_lang() -> str:
    path = Path(DEFAULT_LOCALE)
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key == "LANG":
            return value.strip().strip('"').strip("'")
    return ""


def ensure_locale(locale: str, plan_only: bool) -> None:
    generated = locale_alias(locale) in _output(["locale", "-a"]).split()
    if generated:
        log.sub(f"{locale} already generated")
    elif plan_only:
        log.sub(f"Would enable {locale} in {LOCALE_GEN} and run locale-gen")
    else:
        path = Path(LOCALE_GEN)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        updated = enable_in_locale_gen(text, locale)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
        _check(["locale-gen"])
        log.ok(f"Generated {locale}")

    current = default_lang()
    if current == locale:
        log.sub(f"LANG already {locale}")
    elif plan_only:
        log.sub(f"Would set LANG {current or '(unset)'} -> {locale}")
    else:
        _check(["update-locale", f"LANG={locale}"])
        log.ok(f"LANG set to {locale} (applies to new sessions)")


# ---------------------------------------------------------------------------
# DNS cache
# ---------------------------------------------------------------------------


def resolved_active() -> bool:
    return _run(["systemctl", "is-active", "--quiet", RESOLVED], check=False).returncode == 0


def effective_cache() -> str:
    """The `Cache=` systemd-resolved reads: the last one in its merged config.

    `systemd-analyze cat-config` prints the main file and every drop-in in the
    order resolved applies them, so a later drop-in that turns the cache back on
    is caught here rather than trusted away. Unset means resolved's default, yes.
    """
    value = "yes"
    section = ""
    for raw in _output(["systemd-analyze", "cat-config", "systemd/resolved.conf"]).splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, setting = line.partition("=")
        if sep and section == "Resolve" and key.strip() == "Cache":
            value = setting.strip()
    return value


def ensure_dns_cache_off(ctx: InstallContext, plan_only: bool) -> None:
    if not resolved_active():
        log.sub(f"{RESOLVED} not running; nothing caches DNS")
        return

    dest = ctx.file_map[RESOLVED_DROP_IN][0]
    if plan_only:
        if differs(ctx, RESOLVED_DROP_IN):
            log.sub(f"Would update {dest} and restart {RESOLVED}")
        current = effective_cache()
        if current == "no":
            log.ok(f"{RESOLVED} cache already off")
        else:
            log.sub(f"Effective {RESOLVED} Cache would change: {current} -> no")
        return

    changed = files.install(ctx, RESOLVED_DROP_IN)
    current = effective_cache()
    if current != "no":
        raise InstallError(f"{RESOLVED} Cache is {current} after {dest}, expected no")
    if changed:
        _check(["systemctl", "restart", RESOLVED])
        log.ok(f"{RESOLVED} restarted with its cache off")
    else:
        log.sub(f"{RESOLVED} cache already off")


def install(ctx: InstallContext) -> None:
    log.header("Guest Baseline")
    plan_only = env.deploy_flag(ctx, "PLAN_ONLY")
    preflight(ctx)

    log.action("User")
    ensure_user(ctx, plan_only)

    log.action("Sudoers")
    ensure_sudoers(ctx, plan_only)

    log.action("SSH hardening")
    ensure_sshd(ctx, plan_only)

    log.action("Timezone")
    ensure_timezone(ctx.env["TIMEZONE"], plan_only)

    log.action("Locale")
    ensure_locale(ctx.env["LOCALE"], plan_only)

    log.action("DNS cache")
    ensure_dns_cache_off(ctx, plan_only)

    if plan_only:
        log.sub("PLAN_ONLY: nothing written")


if __name__ == "__main__":
    run(install, "Guest Baseline")
