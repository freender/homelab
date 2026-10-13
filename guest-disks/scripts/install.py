#!/usr/bin/env python3
"""Remote installer for the guest-disks module (freender/homelab-ops#156).

Formats and mounts the role disks listed in `build/<host>/disks.conf`
(`key|role|mount|options`, rendered from the guest's `pve-guest` block).

It looks before it writes. Every disk is probed and planned first. If any disk
would need something this installer refuses to do, the run fails before anything
is written to any disk. With `PLAN_ONLY=true` (a dry run) it prints the plan and
stops there.

What it will do, per disk:

* **mkfs ext4 only on a blank disk** -- `blkid -p` finds no signature and the disk
  has no partitions. A disk holding anything else is never reformatted.
* **Own its fstab lines by marker.** Each managed entry follows a
  `# homelab guest-disks: <key>` line. It only rewrites lines it marked. A line it
  did not write that names the same disk or the same mount path is a conflict,
  never something to edit or remove. A marked line for a disk no longer in the
  spec gets a warning and stays.
* **Never hide or move data.** It refuses to mount over a non-empty directory,
  onto a path where something else is already mounted, or a disk that is already
  mounted somewhere else. Moving off a hand-made mount is migration work
  (homelab-ops#158/#159), not something a deploy does by itself.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from homelab_install import env, files, log, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

DISKS_FILE = "disks.conf"
FSTYPE = "ext4"
MARKER_PREFIX = "# homelab guest-disks: "

# Module-level so tests can rebind them under tmp_path.
BY_ID_DIR = "/dev/disk/by-id"
FSTAB = "/etc/fstab"

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run


@dataclass(frozen=True)
class Wanted:
    key: str
    role: str
    mount: str
    options: str

    @property
    def label(self) -> str:
        return f"{self.key} ({self.role}) -> {self.mount}"


@dataclass(frozen=True)
class Probe:
    """What the guest says about one disk right now."""

    by_id: str
    device: str | None
    fstype: str | None = None
    uuid: str | None = None
    partitioned: bool = False
    mounted_at: tuple[str, ...] = ()
    target_source: str | None = None
    target_state: str = "missing"  # missing | empty | busy | not-a-dir


@dataclass(frozen=True)
class FstabEntry:
    source: str
    target: str
    managed_key: str | None


@dataclass
class Plan:
    wanted: Wanted
    probe: Probe
    mkfs: bool = False
    write_fstab: bool = False
    mount: bool = False
    refusals: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.refusals:
            return f"{self.wanted.label}: REFUSED -- {'; '.join(self.refusals)}"
        steps = [
            name
            for name, needed in (
                (f"mkfs {FSTYPE}", self.mkfs),
                ("write fstab entry", self.write_fstab),
                ("mount", self.mount),
            )
            if needed
        ]
        return f"{self.wanted.label}: {', '.join(steps) if steps else 'in place'}"


# ---------------------------------------------------------------------------
# Pure: parse and decide
# ---------------------------------------------------------------------------


def parse_disks(text: str) -> list[Wanted]:
    wanted: list[Wanted] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("|")
        if len(parts) != 4 or not all(parts):
            raise InstallError(f"malformed {DISKS_FILE} line: {raw!r}")
        key, role, mount, options = parts
        if not mount.startswith("/") or mount == "/":
            raise InstallError(f"{DISKS_FILE}: {key} mount must be an absolute path below /")
        wanted.append(Wanted(key, role, mount, options))
    if not wanted:
        raise InstallError(f"{DISKS_FILE} lists no disks; refusing to run on an empty render")
    return wanted


def parse_fstab(text: str) -> list[FstabEntry]:
    entries: list[FstabEntry] = []
    marker: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(MARKER_PREFIX):
            marker = line[len(MARKER_PREFIX) :].strip()
            continue
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 2:
            entries.append(FstabEntry(fields[0], fields[1], marker))
        marker = None
    return entries


def fstab_line(wanted: Wanted, uuid: str) -> str:
    return f"UUID={uuid} {wanted.mount} {FSTYPE} {wanted.options} 0 2"


def disk_sources(probe: Probe) -> set[str]:
    """Every fstab spelling that names this disk."""
    sources = {probe.by_id}
    if probe.device:
        sources.add(probe.device)
    if probe.uuid:
        sources.update({f"UUID={probe.uuid}", f"/dev/disk/by-uuid/{probe.uuid}"})
    return sources


def decide(wanted: Wanted, probe: Probe, fstab: list[FstabEntry]) -> Plan:
    plan = Plan(wanted, probe)
    if probe.device is None:
        plan.refusals.append(f"no disk at {probe.by_id}; attach it in pve-guest first")
        return plan
    if probe.partitioned:
        plan.refusals.append(f"{probe.device} has a partition table; only whole-disk {FSTYPE}")
    elif probe.fstype not in (None, FSTYPE):
        plan.refusals.append(f"{probe.device} holds {probe.fstype}, not {FSTYPE}; not reformatted")
    plan.mkfs = probe.fstype is None and not probe.partitioned
    _check_fstab(plan, fstab)
    _check_mounts(plan)
    return plan


def _check_fstab(plan: Plan, fstab: list[FstabEntry]) -> None:
    wanted, probe = plan.wanted, plan.probe
    sources = disk_sources(probe)
    current: FstabEntry | None = None
    for entry in fstab:
        if entry.managed_key == wanted.key:
            current = entry
        elif entry.source in sources:
            plan.refusals.append(f"fstab already lists this disk at {entry.target}")
        elif entry.target == wanted.mount:
            plan.refusals.append(f"fstab already mounts {entry.source} at {wanted.mount}")
    plan.write_fstab = plan.mkfs or current is None or not _entry_matches(current, wanted, probe)


def _entry_matches(entry: FstabEntry, wanted: Wanted, probe: Probe) -> bool:
    return entry.source == f"UUID={probe.uuid}" and entry.target == wanted.mount


def _check_mounts(plan: Plan) -> None:
    wanted, probe = plan.wanted, plan.probe
    elsewhere = [target for target in probe.mounted_at if target != wanted.mount]
    if elsewhere:
        plan.refusals.append(f"already mounted at {', '.join(elsewhere)}; unmount it first")
    if wanted.mount in probe.mounted_at:
        return
    if probe.target_source is not None:
        plan.refusals.append(f"{probe.target_source} is already mounted at {wanted.mount}")
    elif probe.target_state == "busy":
        plan.refusals.append(f"{wanted.mount} is not empty; mounting would hide its files")
    elif probe.target_state == "not-a-dir":
        plan.refusals.append(f"{wanted.mount} exists and is not a directory")
    plan.mount = True


def render_fstab(text: str, lines: dict[str, str]) -> str:
    """`text` with each key's marked entry replaced by `lines[key]`, or appended."""
    out: list[str] = []
    pending = set(lines)
    skip_next = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if skip_next and stripped and not stripped.startswith("#"):
            skip_next = False
            continue
        key = stripped[len(MARKER_PREFIX) :].strip() if stripped.startswith(MARKER_PREFIX) else None
        if key in lines:
            out.extend([raw, lines[key]])
            pending.discard(key)
            skip_next = True
            continue
        out.append(raw)
    for key in sorted(pending):
        out.extend([f"{MARKER_PREFIX}{key}", lines[key]])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Live reads
# ---------------------------------------------------------------------------


def _output(argv: list[str]) -> str | None:
    """stdout, or None when the command reports nothing found (non-zero exit)."""
    result = _run(argv, check=False, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def blkid(device: str) -> dict[str, str]:
    out = _output(["blkid", "-p", "-o", "export", device]) or ""
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def has_partitions(device: str) -> bool:
    out = _output(["lsblk", "-nr", "-o", "TYPE", device]) or ""
    return "part" in out.split()


def mounted_targets(device: str) -> tuple[str, ...]:
    out = _output(["findmnt", "-nr", "-o", "TARGET", "--source", device]) or ""
    return tuple(line for line in out.splitlines() if line)


def target_source(mount: str) -> str | None:
    out = (_output(["findmnt", "-nr", "-o", "SOURCE", "--mountpoint", mount]) or "").strip()
    return out or None


def target_state(mount: str) -> str:
    path = Path(mount)
    if not path.exists():
        return "missing"
    if not path.is_dir():
        return "not-a-dir"
    return "busy" if any(path.iterdir()) else "empty"


def probe(wanted: Wanted) -> Probe:
    by_id = f"{BY_ID_DIR}/scsi-0QEMU_QEMU_HARDDISK_drive-{wanted.key}"
    if not Path(by_id).exists():
        return Probe(by_id=by_id, device=None)
    device = os.path.realpath(by_id)
    info = blkid(device)
    return Probe(
        by_id=by_id,
        device=device,
        fstype=info.get("TYPE"),
        uuid=info.get("UUID"),
        partitioned="PTTYPE" in info or has_partitions(device),
        mounted_at=mounted_targets(device),
        target_source=target_source(wanted.mount),
        target_state=target_state(wanted.mount),
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def make_filesystem(plan: Plan) -> str:
    device = plan.probe.device
    assert device is not None
    log.sub(f"mkfs.{FSTYPE} {device} ({plan.wanted.key}, blank)")
    # -F: blkid -p just found no signature, so mke2fs's whole-disk prompt is moot.
    _run([f"mkfs.{FSTYPE}", "-q", "-F", device], check=True)
    uuid = blkid(device).get("UUID")
    if not uuid:
        raise InstallError(f"{device} has no UUID after mkfs")
    return uuid


def apply(ctx: InstallContext, plans: list[Plan]) -> None:
    lines: dict[str, str] = {}
    for plan in plans:
        uuid = make_filesystem(plan) if plan.mkfs else plan.probe.uuid
        if plan.write_fstab:
            assert uuid is not None
            lines[plan.wanted.key] = fstab_line(plan.wanted, uuid)
    if lines:
        fstab = Path(FSTAB)
        files.back_up(FSTAB)
        fstab.write_text(render_fstab(fstab.read_text(encoding="utf-8"), lines), encoding="utf-8")
        log.ok(f"fstab: {', '.join(sorted(lines))}")
        systemd.daemon_reload(ctx)
    for plan in plans:
        if plan.mount:
            Path(plan.wanted.mount).mkdir(mode=0o755, parents=True, exist_ok=True)
            _run(["mount", plan.wanted.mount], check=True)
        device = plan.probe.device
        assert device is not None
        if plan.wanted.mount not in mounted_targets(device):
            raise InstallError(f"{plan.wanted.label}: not mounted after deploy")
        log.ok(f"{plan.wanted.label}: mounted")


def install(ctx: InstallContext) -> None:
    log.header("Guest Disks")
    wanted = parse_disks((ctx.build_dir / DISKS_FILE).read_text(encoding="utf-8"))
    plan_only = env.deploy_flag(ctx, "PLAN_ONLY")
    fstab_text = Path(FSTAB).read_text(encoding="utf-8")
    fstab = parse_fstab(fstab_text)

    log.action("Disk plan")
    plans = [decide(item, probe(item), fstab) for item in wanted]
    for plan in plans:
        log.sub(plan.summary())
    known = {item.key for item in wanted}
    for entry in fstab:
        if entry.managed_key and entry.managed_key not in known:
            log.warn(f"fstab has a guest-disks entry for {entry.managed_key}, no longer in spec")

    refused = [plan for plan in plans if plan.refusals]
    if refused:
        raise InstallError(f"refusing to touch any disk: {len(refused)} disk(s) refused above")
    if plan_only:
        log.sub("PLAN_ONLY: nothing written")
        return
    log.action("Applying")
    apply(ctx, plans)


if __name__ == "__main__":
    run(install, "Guest Disks")
