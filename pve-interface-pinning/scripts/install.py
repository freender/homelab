#!/usr/bin/env python3
"""Remote installer for the pve-interface-pinning module (freender/homelab-ops#30).

Installs the systemd `.link` files that pin PVE node NIC names by MAC, plus the
Wake-on-LAN oneshot that re-applies `wol g` at boot. Every destination comes from
`build/<host>/file-map.conf`.

**Nothing here renames a live interface.** systemd applies a `.link` file when udev
first sees the device, which on a running node means the next boot. That is why
this module is network-critical without ever bouncing a link: a bad pin is only
discovered after a reboot, on a host that may no longer be reachable. The
orchestrator's golden render and postinstall-alignment checks are the guard; this
installer's job is to install exactly what was rendered and say clearly when a
reboot would change a name.

Worth knowing before reading:

* **The link list is the file map.** The bash read a second list,
  `link-files.conf`, written alongside the map from the same pins. Two lists that
  must agree is how `vmalert-rules` ended up checking six of sixteen files; the
  `.link` entries in the map are now the only one.
* **The reboot warning compares content, not `files.install`'s result.** Install
  reports a change on every `--force` deploy, and a "pinned names changed" warning
  that fires on a forced no-op teaches the operator to ignore the one message
  that says the next reboot is risky.
* **A changed WOL file re-runs the oneshot.** The bash ran `enable --now`, which
  does nothing to an active `RemainAfterExit` unit, so a new WOL interface was not
  armed until the next boot. Re-running it only runs `ethtool -s <iface> wol g`.
* **The competing-rule scan stays warn-only**, and now parses keys instead of
  grepping: `grep "Name=nic0$"` also matched `OriginalName=nic0` and
  `AlternativeName=nic0`. It visits `/usr/lib/systemd/network` once -- `/lib` is a
  symlink to it on every node -- and skips any file named like one of ours, which
  the `/etc` copy shadows by filename.
* **The post-install "expected link file was not installed" check is not
  ported.** `files.install` either writes the destination or raises.
"""

from __future__ import annotations

import filecmp
import subprocess
from pathlib import Path

from homelab_install import files, log, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

WOL_SERVICE = "homelab-interface-wol.service"
WOL_CONFIG = "interface-wol.conf"
WOL_FILES = (WOL_CONFIG, "homelab-interface-wol", WOL_SERVICE)
LINK_SUFFIX = ".link"

# systemd merges `.link` files across these by filename, highest priority first.
# Module-level so tests can rebind them under tmp_path.
NETWORK_DIRS = (
    "/etc/systemd/network",
    "/run/systemd/network",
    "/usr/lib/systemd/network",
    "/lib/systemd/network",
)
LEGACY_UDEV_RULE = "/etc/udev/rules.d/70-persistent-net.rules"

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run


def link_names(ctx: InstallContext) -> list[str]:
    names = [name for name in ctx.file_map if name.endswith(LINK_SUFFIX)]
    if not names:
        # The orchestrator refuses a host with no pins, so an empty list here is
        # a broken render -- and would make the reboot check vacuously "unchanged".
        raise InstallError("file map lists no .link files; refusing to deploy an empty pin set")
    # Checked up front because the competing-rule scan reads these before any
    # `files.install` would get the chance to report a missing source cleanly.
    missing = [name for name in names if not (ctx.build_dir / name).is_file()]
    if missing:
        raise InstallError(f"missing source file(s) for pinned links: {', '.join(missing)}")
    return names


def parse_link(text: str) -> tuple[set[str], set[str]]:
    """The MACs a `.link` file matches and the names it assigns.

    Exact keys only. `MACAddress=` may list several addresses; `Name=` is the one
    assignment -- `OriginalName=` and `AlternativeName=` are different keys.
    """
    macs: set[str] = set()
    names: set[str] = set()
    for raw in text.splitlines():
        key, sep, value = raw.strip().partition("=")
        if not sep or key.startswith(("#", ";")):
            continue
        if key in ("MACAddress", "PermanentMACAddress"):
            macs.update(mac.lower() for mac in value.split())
        elif key == "Name" and value.strip():
            names.add(value.strip())
    return macs, names


def _foreign_link_files(managed: set[str]) -> list[Path]:
    seen: set[Path] = set()
    found: list[Path] = []
    for directory in NETWORK_DIRS:
        resolved = Path(directory).resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        candidates = sorted(resolved.glob(f"*{LINK_SUFFIX}"))
        found.extend(path for path in candidates if path.name not in managed)
    return found


def warn_competing_rules(ctx: InstallContext, links: list[str]) -> None:
    """Warn about other on-host rules that could claim a pinned MAC or name.

    Best-effort and never fatal: a false positive must not block a deploy.
    """
    log.action("Competing interface-naming rules")
    if Path(LEGACY_UDEV_RULE).exists():
        log.warn(f"legacy {LEGACY_UDEV_RULE} present; may race with pinned .link files")

    pins = [parse_link((ctx.build_dir / name).read_text(encoding="utf-8")) for name in links]
    for path in _foreign_link_files(set(links)):
        try:
            macs, names = parse_link(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for pin_macs, pin_names in pins:
            target = ", ".join(sorted(pin_names))
            if pin_macs & macs:
                mac = ", ".join(sorted(pin_macs & macs))
                log.warn(
                    f"foreign link file {path} also matches pinned MAC {mac} "
                    f"(target name {target}); may race with homelab pin"
                )
            elif pin_names & names:
                log.warn(
                    f"foreign link file {path} also targets name {target}; "
                    "may collide with homelab pin"
                )


def _differs(ctx: InstallContext, name: str) -> bool:
    src = ctx.build_dir / name
    dest = Path(ctx.file_map[name][0])
    return not (src.is_file() and dest.is_file() and filecmp.cmp(src, dest, shallow=False))


def install(ctx: InstallContext) -> None:
    log.header("PVE Interface Pinning")
    links = link_names(ctx)
    warn_competing_rules(ctx, links)

    log.action("PVE interface pinning")
    renamed = [name for name in links if _differs(ctx, name)]
    # Every file, never short-circuited: one unchanged link must not skip the next.
    if any([files.install(ctx, name) for name in links]):
        result = _run(["udevadm", "control", "--reload"], check=False)
        if result.returncode != 0:
            log.warn("failed to reload udev rules")

    wol_changed = any([files.install(ctx, name) for name in WOL_FILES])
    if (ctx.build_dir / WOL_CONFIG).read_text(encoding="utf-8").strip():
        systemd.ensure_running(ctx, WOL_SERVICE, changed=wol_changed)
    else:
        if wol_changed:
            systemd.daemon_reload(ctx)
        systemd.ensure_stopped(ctx, WOL_SERVICE)
        log.sub("No WOL interfaces configured")

    if renamed:
        log.warn(
            "Pinned interface name(s) changed; systemd-networkd applies renames on next boot only "
            "-- review /etc/network/interfaces before rebooting"
        )
    else:
        log.sub("Pinned interface names unchanged; no reboot needed for interface naming")


if __name__ == "__main__":
    run(install, "PVE Interface Pinning")
