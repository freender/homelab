#!/usr/bin/env python3
"""Remote installer for the pve-gpu-passthrough module (freender/homelab-ops#30).

Writes the systemd-boot kernel cmdline, the GPU blacklist and vfio-pci binding
configs, and the VFIO module list, then rebuilds the initramfs and refreshes the
ESPs when those inputs changed. **This module can make a node unbootable**: a
cmdline whose `root=` does not resolve does not come back. Nothing here reboots;
every change takes effect on the next boot, which is the operator's decision.

Worth knowing before reading:

* **Every refusal happens before anything is written.** The cmdline token, the
  root dataset and `/etc/kernel/cmdline` itself are all checked first. The bash
  backed files up before checking, and deployed the emergency removal script
  last -- after the boot config it exists to undo.
* **The emergency removal script is installed first**, for that reason.
* **Backups only on change.** The bash backed up `/etc/kernel/cmdline` and
  `/etc/modules` unconditionally, so every no-op deploy wrote a `.bak` and
  pruned a real one: all four nodes held three copies of an identical cmdline,
  and the pre-change copy a human would want after a bad boot was gone after
  three routine deploys.
* **No backups inside `/etc/modprobe.d/` or `/etc/modules-load.d/`.** AGENTS.md
  forbids timestamped copies in active include directories. The managed files
  there are rendered from the repo, so git is their backup; the legacy
  `blacklist.conf` edit only comments lines out, so the original text stays in
  the file.
* **A failed `update-initramfs` or `proxmox-boot-tool refresh` says how to
  retry.** The files are already in place by then, so a plain redeploy sees no
  change and skips both commands for good. The bash had the same trap and said
  nothing. `--force` rewrites the cmdline and always re-runs both.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from homelab_install import files, log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

REQUIRED_ROOT_TOKEN = "root=ZFS=rpool/ROOT/pve-1"
ROOT_DATASET = REQUIRED_ROOT_TOKEN.removeprefix("root=ZFS=")
LEGACY_DRIVERS = ("i915", "nvidia", "nouveau")
MIGRATED_MARKER = "  # Migrated by pve-gpu-passthrough"
REMOVAL_SCRIPT = "remove-local.sh"

# Module-level so tests can rebind them under tmp_path.
KERNEL_CMDLINE = "/etc/kernel/cmdline"
ETC_MODULES = "/etc/modules"
LEGACY_BLACKLIST = "/etc/modprobe.d/blacklist.conf"
REMOVAL_SCRIPT_DEST = "/root/pve-gpu-passthrough-remove.sh"
# build name -> destination. A name absent from the build means the host's
# inventory no longer asks for it, and the destination is removed.
MANAGED_FILES = {
    "blacklist.conf": "/etc/modprobe.d/homelab-gpu-blacklist.conf",
    "vfio.conf": "/etc/modprobe.d/vfio.conf",
    "modules": "/etc/modules-load.d/vfio.conf",
}

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run


def rendered_cmdline(ctx: InstallContext) -> Path:
    """The build cmdline, refused unless it is one line carrying the root token.

    Installed byte-for-byte, so a second line would reach `/etc/kernel/cmdline`
    -- the bash only ever read the first. The orchestrator checks the token too;
    this is the last check before the write, on the host that has to boot.
    """
    src = ctx.build_dir / "cmdline"
    if not src.is_file():
        raise InstallError(f"missing source file: {src}")
    lines = src.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise InstallError(f"{src} must be exactly one line, found {len(lines)}")
    if REQUIRED_ROOT_TOKEN not in lines[0].split():
        raise InstallError(
            f"refusing to write {KERNEL_CMDLINE} without required token: {REQUIRED_ROOT_TOKEN}"
        )
    return src


def preflight(ctx: InstallContext) -> Path:
    src = rendered_cmdline(ctx)
    if not Path(KERNEL_CMDLINE).is_file():
        raise InstallError(f"{KERNEL_CMDLINE} not found (systemd-boot required)")
    result = _run(
        ["zfs", "list", "-H", "-o", "name", ROOT_DATASET],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise InstallError(f"required ZFS dataset not found: {ROOT_DATASET}")
    return src


def migrate_legacy_blacklist() -> bool:
    """Comment out GPU driver blacklists left in the stock `blacklist.conf`.

    Prefix match, as the bash's `^blacklist (i915|nvidia|nouveau)` was, so
    `blacklist nvidiafb` is migrated too. The whole line is kept ahead of the
    marker; `sed 's/^blacklist i915/# &  # Migrated.../'` put any trailing text
    after it. No host has a line with trailing text, so nothing on disk differs.
    """
    path = Path(LEGACY_BLACKLIST)
    if not path.is_file():
        return False
    prefixes = tuple(f"blacklist {driver}" for driver in LEGACY_DRIVERS)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    migrated = [
        f"# {line.rstrip(chr(10))}{MIGRATED_MARKER}\n" if line.startswith(prefixes) else line
        for line in lines
    ]
    if migrated == lines:
        return False
    path.write_text("".join(migrated), encoding="utf-8")
    log.sub(f"Commented out GPU driver blacklists in {LEGACY_BLACKLIST}")
    return True


def sync_managed_file(ctx: InstallContext, name: str, dest: str) -> bool:
    src = ctx.build_dir / name
    if src.is_file():
        return files.install_from(ctx, src, dest, "644")
    return files.remove(ctx, dest, reason="not rendered for this host")


def clean_etc_modules(ctx: InstallContext) -> bool:
    """Drop `vfio*` lines from `/etc/modules`; they live in modules-load.d now.

    Rewritten in place, so its mode and owner survive -- the bash `mv`'d a
    `/tmp` file over it, which also left that file behind whenever it failed.
    """
    path = Path(ETC_MODULES)
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith("vfio")]
    if kept == lines:
        return False
    files.back_up(ETC_MODULES)
    path.write_text("".join(kept), encoding="utf-8")
    log.sub(f"Removed vfio entries from {ETC_MODULES}")
    ctx.changes.record(ETC_MODULES)
    return True


def run_boot_command(argv: list[str]) -> None:
    result = _run(argv, check=False)
    if result.returncode != 0:
        raise InstallError(
            f"{' '.join(argv)} failed (exit {result.returncode}). The config files are "
            "already in place, so a plain redeploy will skip this step; "
            "re-run the deploy with --force"
        )


def install(ctx: InstallContext) -> None:
    log.header("PVE GPU Passthrough")
    cmdline_src = preflight(ctx)

    log.action("Emergency removal script")
    files.install_from(ctx, ctx.script_dir / "scripts" / REMOVAL_SCRIPT, REMOVAL_SCRIPT_DEST, "755")

    log.action("systemd-boot cmdline")
    boot_changed = files.install_from(ctx, cmdline_src, KERNEL_CMDLINE, "644", backup=True)

    log.action("Modprobe configs and VFIO modules")
    # Every step runs; a list, not a generator, so `any` cannot short-circuit one.
    module_changes = [
        migrate_legacy_blacklist(),
        *[sync_managed_file(ctx, name, dest) for name, dest in MANAGED_FILES.items()],
        clean_etc_modules(ctx),
    ]

    # `--force` re-runs it even with nothing to rewrite: on a host that renders no
    # modprobe files, that is the only retry after a failed run that removed one.
    if ctx.force_update or any(module_changes):
        log.action("Updating initramfs")
        run_boot_command(["update-initramfs", "-u", "-k", "all"])
    else:
        log.sub("No module changes detected; skipping initramfs update")

    if boot_changed:
        log.action("Refreshing systemd-boot")
        run_boot_command(["proxmox-boot-tool", "refresh"])
    else:
        log.sub("Kernel cmdline unchanged; skipping systemd-boot refresh")

    if boot_changed or any(module_changes):
        log.warn("GPU passthrough boot config changed; takes effect on next reboot")


if __name__ == "__main__":
    run(install, "PVE GPU Passthrough")
