#!/usr/bin/env python3
"""Remote installer for the metrics-exporters module (freender/homelab-ops#30).

Installs the native node_exporter config on every host. On bare metal it also
installs the distro smartctl_exporter with its unit override, and the ZFS pool,
disk-label and pending-reboot textfile exporters. Where inventory asks for them it
adds the HBA textfile exporter and the apcupsd and Intel GPU exporters. Everything
it does is derived from the file map; there is no env file.

Behaviour changes from `install.sh`:

* **Every refusal comes before any write.** That covers `/etc/os-release`, every
  staged file-map entry, and a partial exporter: a map that carries a unit without
  its script, or an override without the wait-devices helper it points
  `ExecStartPre` at, now refuses instead of enabling a unit with nothing behind it.
* **A textfile exporter's oneshot runs only when its own files changed.** The bash
  ran every one on every deploy. A changed script, unit or config still takes
  effect at once rather than at the next timer tick, which is what the start was
  for. A timer is restarted only when its own unit file changed.
* **A changed exporter is restarted.** The bash never restarted `apcupsd-exporter`
  at all, and restarted `igpu-exporter` for its script or defaults but not its unit.
* **Every inactive unit is named at once** (`systemd.require_active`). The bash
  stopped at the first.
* **The packages are checked through dpkg** (`packages.ensure`), not `command -v`.
  `python3` is no longer checked: this installer is running in it.
* **A retired optional exporter is removed whether or not its timer is still
  there.** The bash left the script and `.prom` behind if the timer was already
  gone, and the textfile collector keeps serving a stale `.prom` forever.
* **A new backports source no longer forces `apt-get update` when the package is
  already installed.** The lists are only refreshed on a run that installs.

**Not ported.** Each was checked and found absent on all 14 hosts: the
self-managed `smartctl-exporter` (hyphen) unit, defaults and binary, with the
`apt-cache policy` candidate check that only existed to protect it; the retired
`systemd-failed-textfile-exporter` and `hba-temp-textfile-exporter`; the
`.igpu-exporter.version` sidecar; a Debian backports source on an Ubuntu host; and
a `node-exporter` or `smartctl-exporter` container still bound to the native ports.
No compose file in the repo defines either container any more.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from homelab_install import files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run

OS_RELEASE = "/etc/os-release"
DEV_ZFS = "/dev/zfs"
SYSTEMD_DIR = "/etc/systemd/system"
BIN_DIR = "/usr/local/bin"
DEFAULT_DIR = "/etc/default"
ETC_HOMELAB = "/etc/homelab"
TEXTFILE_DIR = "/var/lib/prometheus/node-exporter"
BACKPORTS_SOURCE = "/etc/apt/sources.list.d/debian-backports.sources"

NODE_EXPORTER = "prometheus-node-exporter"
NODE_UNIT = f"{NODE_EXPORTER}.service"
NODE_DEFAULTS = "node-exporter.defaults"
SMARTCTL_PACKAGE = "prometheus-smartctl-exporter"
# The packaged unit is underscored, unlike the hyphenated one this module used to ship.
SMARTCTL_UNIT = "smartctl_exporter.service"
SMARTCTL_OVERRIDE = "smartctl-exporter-override.conf"
SMARTCTL_WAIT = "smartctl-exporter-wait-devices"

APCUPSD = ("apcupsd-exporter.py", "apcupsd-exporter.service", "apcupsd-exporter.env")
IGPU = ("igpu-exporter.py", "igpu-exporter.service", "igpu-exporter.defaults")

# Textfile exporters: file-map script entry -> (unit stem, configs it reads).
TEXTFILE_EXPORTERS = {
    "zfs-pool-textfile-exporter": ("zfs-pool-textfile-exporter", ("zfs-expected-pools.conf",)),
    "hba-textfile-exporter.py": ("hba-textfile-exporter", ()),
    "reboot-textfile-exporter": ("reboot-textfile-exporter", ("pve-patch-statuses.conf",)),
    "boot-entry-textfile-exporter": ("boot-entry-textfile-exporter", ()),
    "disk-label-textfile-exporter.py": ("disk-label-textfile-exporter", ("disk-labels.conf",)),
}

# Entries that only make sense together. A map carries all of a group or none of it.
GROUPS = (
    APCUPSD,
    IGPU,
    (SMARTCTL_OVERRIDE, SMARTCTL_WAIT),
    *(
        (script, f"{stem}.service", f"{stem}.timer")
        for script, (stem, _configs) in TEXTFILE_EXPORTERS.items()
    ),
)


def os_release() -> tuple[str, str]:
    """`(ID, VERSION_CODENAME)`, refusing when either is missing."""
    path = Path(OS_RELEASE)
    if not path.is_file():
        raise InstallError(f"cannot read {OS_RELEASE}")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, raw = line.partition("=")
        if sep and key.strip():
            tokens = shlex.split(raw, comments=True)
            values[key.strip()] = tokens[0] if tokens else ""
    os_id, codename = values.get("ID", ""), values.get("VERSION_CODENAME", "")
    if not os_id or not codename:
        raise InstallError(f"ID/VERSION_CODENAME missing from {OS_RELEASE}")
    return os_id, codename


def preflight(ctx: InstallContext) -> tuple[str, str]:
    """Every check that can refuse the deploy. Returns `(ID, VERSION_CODENAME)`."""
    release = os_release()
    if NODE_DEFAULTS not in ctx.file_map:
        raise InstallError(f"missing file-map entries: {NODE_DEFAULTS}")
    for group in GROUPS:
        mapped = [name for name in group if name in ctx.file_map]
        if mapped and len(mapped) != len(group):
            unmapped = [name for name in group if name not in ctx.file_map]
            raise InstallError(
                f"partial exporter in file map: {', '.join(mapped)} "
                f"without {', '.join(unmapped)}"
            )
    missing = [name for name in ctx.file_map if not (ctx.build_dir / name).is_file()]
    if missing:
        raise InstallError(f"missing staged files: {', '.join(missing)}")
    return release


def backports_source(codename: str) -> str:
    # Rendered from the running release rather than hardcoded, so the suite follows
    # the next Debian major upgrade.
    return (
        "# Managed by homelab (metrics-exporters): provides prometheus-smartctl-exporter,\n"
        "# which Debian stable ships only via backports.\n"
        "Types: deb\n"
        "URIs: http://deb.debian.org/debian/\n"
        f"Suites: {codename}-backports\n"
        "Components: main\n"
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
    )


def ensure_smartctl_exporter(ctx: InstallContext, os_id: str, codename: str) -> None:
    """Install the distro smartctl_exporter.

    Ubuntu carries it in the main archive. Debian stable has it only in backports,
    which is `NotAutomatic` + `ButAutomaticUpgrades`: enabling it pulls in nothing
    on its own, but keeps this package upgraded once installed.
    """
    if os_id != "debian":
        packages.ensure(ctx, SMARTCTL_PACKAGE)
        return

    content = backports_source(codename)
    source = Path(BACKPORTS_SOURCE)
    if not source.is_file() or source.read_text(encoding="utf-8") != content:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content, encoding="utf-8")
        source.chmod(0o644)
        log.ok(f"Enabled {codename}-backports for {SMARTCTL_PACKAGE}")
        packages.sources_changed(ctx)
    packages.ensure(ctx, SMARTCTL_PACKAGE, target_release=f"{codename}-backports")


def _retire(ctx: InstallContext, units: tuple[str, ...], paths: tuple[str, ...], what: str) -> bool:
    retired = False
    for unit in units:
        retired |= systemd.retire_unit(ctx, unit, f"{SYSTEMD_DIR}/{unit}")
    for path in paths:
        retired |= files.remove(ctx, path, f"{what} not configured")
    return retired


def retire_unconfigured(ctx: InstallContext) -> bool:
    """Remove every optional exporter and config the file map no longer carries.

    A leftover `.prom` has to go with its exporter: the textfile collector serves
    whatever is in that directory whether or not anything still writes it. Timers
    are retired before their services. Returns True if a unit was removed.
    """
    retired = False
    if APCUPSD[0] not in ctx.file_map:
        retired |= _retire(
            ctx,
            ("apcupsd-exporter.service",),
            (f"{DEFAULT_DIR}/apcupsd-exporter", f"{BIN_DIR}/apcupsd-exporter"),
            "apcupsd exporter",
        )
    if IGPU[0] not in ctx.file_map:
        retired |= _retire(
            ctx,
            ("igpu-exporter.service",),
            (f"{DEFAULT_DIR}/igpu-exporter", f"{BIN_DIR}/igpu-exporter"),
            "Intel GPU exporter",
        )
    for script, prom in (
        ("hba-textfile-exporter.py", "hba.prom"),
        ("disk-label-textfile-exporter.py", "disk-labels.prom"),
    ):
        if script not in ctx.file_map:
            stem = TEXTFILE_EXPORTERS[script][0]
            retired |= _retire(
                ctx,
                (f"{stem}.timer", f"{stem}.service"),
                (f"{BIN_DIR}/{stem}", f"{TEXTFILE_DIR}/{prom}"),
                stem,
            )
    if "disk-labels.conf" not in ctx.file_map:
        files.remove(ctx, f"{ETC_HOMELAB}/disk-labels.conf", "no overrides configured")
    if "pve-patch-statuses.conf" not in ctx.file_map:
        files.remove(ctx, f"{ETC_HOMELAB}/pve-patch-statuses.conf", "no patches configured")
        files.remove(ctx, f"{TEXTFILE_DIR}/pve-patches.prom", "no patches configured")
    return retired


def mask_unwanted(ctx: InstallContext) -> None:
    """Mask units that can never succeed here, so they cannot mask real failures.

    This module owns failed-unit visibility, so it keeps that signal clean.
    `openipmi` fails wherever there is no BMC and in every LXC guest; masking it
    here covers the hosts that get neither `pve-postinstall` nor `ubuntu-setup`.
    The container checks are runtime facts, not host lists, so bare metal can never
    trip them: `nvmf-autoconnect` needs a module a container cannot load, and the
    ZFS units are gated on `/dev/zfs`, never on container-ness, so masking cannot
    fire on a host that mounts ZFS.
    """
    log.action("Unwanted default services")
    systemd.mask(ctx, "openipmi.service")
    in_container = (
        _run(["systemd-detect-virt", "--container", "--quiet"], check=False).returncode == 0
    )
    if not in_container:
        return
    systemd.mask(ctx, "nvmf-autoconnect.service", "no nvme-fabrics in a container")
    if not Path(DEV_ZFS).exists():
        for unit in ("zfs-mount.service", "zfs-share.service", "zfs-zed.service"):
            systemd.mask(ctx, unit, "no /dev/zfs")


def start_services(ctx: InstallContext) -> list[str]:
    """Enable, restart and run what the file map carries. Returns the units to verify."""
    touched = ctx.changes.touched
    systemd.ensure_running(ctx, NODE_UNIT, changed=touched(NODE_DEFAULTS))
    expected = [NODE_UNIT]

    for script, (stem, configs) in TEXTFILE_EXPORTERS.items():
        if script not in ctx.file_map:
            continue
        timer, service = f"{stem}.timer", f"{stem}.service"
        systemd.ensure_running(ctx, timer, changed=touched(timer))
        if touched(script, service, *configs):
            systemd.run_once(ctx, service)
            log.ok(f"{service} ran")
        expected.append(timer)

    for unit, entries in (
        (SMARTCTL_UNIT, (SMARTCTL_OVERRIDE,)),
        ("igpu-exporter.service", IGPU),
        ("apcupsd-exporter.service", APCUPSD),
    ):
        if entries[0] in ctx.file_map:
            systemd.ensure_running(ctx, unit, changed=touched(*entries))
            expected.append(unit)
    return expected


def install(ctx: InstallContext) -> None:
    log.header("Prometheus Metrics Exporters")
    os_id, codename = preflight(ctx)

    log.action("Packages")
    wanted = [NODE_EXPORTER]
    if IGPU[0] in ctx.file_map:
        wanted.append("intel-gpu-tools")
    packages.ensure(ctx, *wanted)
    # smartctl_exporter needs real disk device nodes, so a guest's map has no override.
    if SMARTCTL_OVERRIDE in ctx.file_map:
        ensure_smartctl_exporter(ctx, os_id, codename)

    Path(TEXTFILE_DIR).mkdir(parents=True, exist_ok=True)
    retired = retire_unconfigured(ctx)

    log.action("Files")
    files.install_all(ctx)

    mask_unwanted(ctx)

    units = [
        name
        for name in ctx.file_map
        if name.endswith((".service", ".timer")) or name == SMARTCTL_OVERRIDE
    ]
    if retired or ctx.changes.touched(*units):
        systemd.daemon_reload(ctx)

    log.action("Services")
    systemd.require_active(ctx, *start_services(ctx))


if __name__ == "__main__":
    run(install, "Prometheus Metrics Exporters")
