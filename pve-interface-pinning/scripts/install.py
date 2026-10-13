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

On a VM (`VM_GUEST=true`, homelab-ops#170) the module also owns the network
config cloud-init set up on first boot: one `.network` per pin, matched by the
pinned name. The hand-placed `10-lan.network` and cloud-init/netplan only go once
udev confirms that each pin's NIC would get our `.link` and name, so a VM whose
rename is still pending a reboot (riven was `ens18`) is never left with a
`.network` that matches nothing. A `.link` change rebuilds the initramfs, because
Debian's udev hook copies `/etc/systemd/network/*.link` into it. Networking is
never restarted: everything takes effect at the next boot.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from homelab_install import env, files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

WOL_SERVICE = "homelab-interface-wol.service"
WOL_CONFIG = "interface-wol.conf"
WOL_FILES = (WOL_CONFIG, "homelab-interface-wol", WOL_SERVICE)
LINK_SUFFIX = ".link"
NETWORK_SUFFIX = ".network"
NETWORKD = "systemd-networkd.service"
SYS_CLASS_NET = "/sys/class/net"
BOOT_DIR = "/boot"

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
    return not (src.is_file() and dest.is_file() and files.same_content(src, dest))


def _words(ctx: InstallContext, name: str) -> list[str]:
    return ctx.env.get(name, "").split()


def network_names(ctx: InstallContext) -> list[str]:
    names = [name for name in ctx.file_map if name.endswith(NETWORK_SUFFIX)]
    if not names:
        raise InstallError("VM file map lists no .network files; refusing to touch networking")
    return names


def _iface_by_mac(mac: str) -> str | None:
    root = Path(SYS_CLASS_NET)
    if not root.is_dir():
        return None
    for path in sorted(root.iterdir()):
        if not (path / "device").exists():
            continue
        try:
            address = (path / "address").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if address == mac:
            return path.name
    return None


def _udev_link_properties(iface: str) -> dict[str, str]:
    """What udev's `net_setup_link` would apply to `iface` at the next boot."""
    result = _run(
        ["udevadm", "test-builtin", "net_setup_link", f"{SYS_CLASS_NET}/{iface}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    properties: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.startswith("ID_NET_"):
            properties[key] = value
    return properties


def pins_confirmed(ctx: InstallContext, links: list[str]) -> bool:
    """Whether udev would give every pinned NIC our `.link` and name at boot."""
    confirmed = True
    for name in links:
        macs, names = parse_link((ctx.build_dir / name).read_text(encoding="utf-8"))
        target = ", ".join(sorted(names))
        dest = ctx.file_map[name][0]
        for mac in sorted(macs):
            iface = _iface_by_mac(mac)
            if iface is None:
                log.warn(f"no NIC has MAC {mac}; cannot confirm the {target} pin")
                confirmed = False
                continue
            found = _udev_link_properties(iface)
            if found.get("ID_NET_LINK_FILE") == dest and found.get("ID_NET_NAME") == target:
                log.ok(f"udev names {iface} ({mac}) {target} at boot")
                continue
            log.warn(
                f"udev would not name {iface} ({mac}) {target} from {dest}: got "
                f"{found.get('ID_NET_LINK_FILE') or 'no link file'}, "
                f"name {found.get('ID_NET_NAME') or 'none'}"
            )
            confirmed = False
    return confirmed


def _initramfs_lacks_links(links: list[str]) -> bool:
    images = sorted(Path(BOOT_DIR).glob("initrd.img-*"))
    for image in images:
        result = _run(["lsinitramfs", str(image)], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            return True
        listed = set((result.stdout or "").splitlines())
        if any(f"usr/lib/systemd/network/{name}" not in listed for name in links):
            return True
    return False


def refresh_initramfs(links: list[str], links_written: bool) -> None:
    if not links_written and not _initramfs_lacks_links(links):
        log.sub("initramfs already carries the pinned .link files")
        return
    log.action("Rebuilding the initramfs with the pinned .link files")
    if _run(["update-initramfs", "-u", "-k", "all"], check=False).returncode != 0:
        raise InstallError(
            "update-initramfs failed; the .link files are already in place, "
            "so re-run the deploy with --force"
        )
    log.ok("initramfs rebuilt")


def _remove_path(ctx: InstallContext, dest: str) -> bool:
    path = Path(dest)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        log.sub(f"Removed {dest} (cloud-init leftover)")
        ctx.changes.record(dest)
        return True
    return files.remove(ctx, dest, "cloud-init leftover")


def decloud(ctx: InstallContext) -> None:
    purged = packages.purge(ctx, *_words(ctx, "CLOUD_PACKAGES"))
    removed = [dest for dest in _words(ctx, "CLOUD_PATHS") if _remove_path(ctx, dest)]
    if not purged and not removed:
        log.sub("cloud-init and netplan already gone")


def install_vm_network(ctx: InstallContext, links: list[str], links_written: bool) -> None:
    log.action("VM network config")
    for name in network_names(ctx):
        files.install(ctx, name)
    systemd.enable(ctx, NETWORKD)
    refresh_initramfs(links, links_written)

    if not pins_confirmed(ctx, links):
        log.warn("keeping cloud-init and the retired .network files until udev confirms every pin")
        return
    for dest in _words(ctx, "RETIRED_NETWORK_FILES"):
        files.remove(ctx, dest, "replaced by the managed .network")
    decloud(ctx)


def install(ctx: InstallContext) -> None:
    log.header("PVE Interface Pinning")
    links = link_names(ctx)
    warn_competing_rules(ctx, links)

    log.action("PVE interface pinning")
    renamed = [name for name in links if _differs(ctx, name)]
    # Every file, never short-circuited: one unchanged link must not skip the next.
    links_written = any([files.install(ctx, name) for name in links])
    if links_written:
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

    if env.flag(ctx, "VM_GUEST"):
        install_vm_network(ctx, links, links_written)

    if renamed:
        log.warn(
            "Pinned interface name(s) changed; systemd-networkd applies renames on next boot only "
            "-- review /etc/network/interfaces before rebooting"
        )
    else:
        log.sub("Pinned interface names unchanged; no reboot needed for interface naming")


if __name__ == "__main__":
    run(install, "PVE Interface Pinning")
