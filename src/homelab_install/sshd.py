"""sshd drop-in install and effective-config verification.

Moved out of `ubuntu-setup` when `guest-baseline` became its second caller
(freender/homelab-ops#162).

The file being on disk proves nothing: sshd keeps the *first* value it reads for
most keywords, so any `sshd_config.d` file sorting ahead of ours -- cloud-init's
`50-cloud-init.conf` is the usual one -- or a line in `sshd_config` itself wins
silently. `verify_effective` asks `sshd -T` what the daemon will actually use.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import files, log
from .context import InstallContext
from .errors import InstallError

# sshd -T prints the canonical keyword; a drop-in may use a deprecated alias.
KEYWORD_ALIASES = {"challengeresponseauthentication": "kbdinteractiveauthentication"}
# ...and the canonical value: `prohibit-password` is printed as `without-password`.
VALUE_ALIASES = {"prohibit-password": "without-password"}

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run


def expected_settings(path: Path) -> dict[str, str]:
    """`keyword value` pairs from a drop-in, keyed and valued as `sshd -T` prints them."""
    settings: dict[str, str] = {}
    for line in path.read_bytes().decode().splitlines():
        words = line.split()
        if len(words) >= 2 and not words[0].startswith("#"):
            key = words[0].lower()
            value = " ".join(words[1:]).lower()
            settings[KEYWORD_ALIASES.get(key, key)] = VALUE_ALIASES.get(value, value)
    return settings


def effective_settings() -> dict[str, str]:
    result = _run(["sshd", "-T"], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise InstallError(f"sshd -T failed (exit {result.returncode})")
    effective: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition(" ")
        effective[key.lower()] = value.strip().lower()
    return effective


def effective_differences(dropin: Path) -> list[str]:
    """`<key> is <effective>, expected <drop-in value>` for every setting that differs."""
    effective = effective_settings()
    return [
        f"{key} is {effective.get(key, '(unset)')}, expected {value}"
        for key, value in expected_settings(dropin).items()
        if effective.get(key) != value
    ]


def verify_effective(dropin: Path, dest: str) -> None:
    """Fail unless the running config really says what the drop-in at `dest` says."""
    wrong = effective_differences(dropin)
    if wrong:
        raise InstallError(
            f"effective sshd config overrides {dest}: {'; '.join(wrong)}. sshd keeps the first "
            "value it reads, so an earlier file in /etc/ssh/sshd_config.d or sshd_config wins"
        )
    log.ok("Effective sshd config matches the hardening drop-in")


def reload() -> None:
    """Make sshd pick up the new config without dropping open sessions.

    Under socket activation (`ssh.socket` owns port 22, as on deepstone and
    tower) a reload kills sshd: on SIGHUP it re-executes and tries to bind the
    port itself, fails with "Cannot bind any address", and the unit fails --
    sometimes after systemd has already reported the reload as done. A restart
    instead gets the listening socket handed over by systemd, and the unit's
    `KillMode=process` leaves established sessions running either way.
    """
    socket_activated = (
        _run(["systemctl", "is-active", "--quiet", "ssh.socket"], check=False).returncode == 0
    )
    argv = (
        ["systemctl", "restart", "ssh.service"]
        if socket_activated
        else ["systemctl", "reload", "ssh"]
    )
    result = _run(argv, check=False, stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        raise InstallError(f"{' '.join(argv)} failed (exit {result.returncode})")
    log.ok("SSH restarted (socket-activated)" if socket_activated else "SSH reloaded")


def harden(ctx: InstallContext, name: str, retired: tuple[str, ...] = ()) -> None:
    """Install file-map entry `name` as a drop-in, validated by `sshd -t`; remove
    `retired` drop-ins it supersedes; reload on any change; verify the result.

    A retired file is only removed once the new one is in place and accepted, so
    a host is never left with neither.
    """
    changed = files.install_validated(ctx, name, ["sshd", "-t"])
    for path in retired:
        changed = files.remove(ctx, path, "superseded") or changed
    if changed:
        reload()
    verify_effective(ctx.build_dir / name, ctx.file_map[name][0])
