#!/usr/bin/env python3
"""Write human-readable disk names for the node_exporter textfile collector.

Grafana's disk panels graph one series per physical disk, and a raw serial
("S7HGNJ0Y413560K") is unreadable in a legend. That used to be solved in the
dashboard: ~966 hand-written field overrides mapping serial -> friendly name,
duplicated across ten panels, so every disk swap meant editing ten panels by
hand. The map had already rotted -- three serials belonged to disks that no
longer existed, and the same serial carried different names on different panels.

This exporter removes the map instead of relocating it. A name is
`<pool> [<position>]`, all of which is readable from the running system, so
nothing has to be written down by a human and no hardware identifier ever enters
the repo. A disk swap is picked up on the next timer tick with no edit anywhere.

The name deliberately encodes as little as possible:

  - No capacity. It disambiguates nothing (clovis has five identical 10TB
    members) and printing it needs a rounding heuristic that is wrong for
    exactly the drives that are sized just below a round number -- a 1.92TB
    enterprise SSD reads as "2TB". The exact byte count is published as a label
    instead, where it needs no rounding decision at all.
  - No vdev geometry. "Z1" (raidz1) would have made a pool rebuilt as raidz2
    rename every one of its disks and orphan nine series, and it is not what
    anyone needs to locate a disk. It also cost the entire indentation-aware
    tree parse this file used to carry; keyed on the pool instead, position is
    just the member's ordinal.
  - No host, and no prettification of the pool name. Every other panel in the
    dashboard builds its legend as `{{host}} <thing>` from the `host` label the
    scrape config attaches, so a name that carried its own Title-cased host was
    the one series in the dashboard that could never match its neighbours --
    "Ace Vault D1" sitting next to "ace vm-flash" and "ace - SAS9207-8i". The
    pool name is emitted verbatim for the same reason: `vault-hdd` is what
    `zpool status` calls it and what homelab_zpool_* already labels it, so a
    cosmetic `vault-hdd` -> `Vault` map only created a fourth spelling of one
    pool. Panels group by (host, disk_label) and render `{{host}} {{disk_label}}`.

Deliberately no `serial` label. Serial is not the join key -- queries join on
(host, device) -- and it is actively ambiguous: for a USB-attached disk the
kernel and smartctl disagree about it. cottonwood sdb is Y93814AW0JNFS6S to
node_exporter and S6SFNJ0WA41839Y to smartctl, and cinci sda reports a bridge
placeholder of 0000000000000000. The old dashboard needed *two* override
entries per USB disk for exactly this reason. node_disk_info still carries the
serial for anyone who wants it, joinable on the same (host, device).

Two metrics are emitted with identical labels but different `device` values,
because the two exporters name the same disk differently:

  homelab_disk_label        device=nvme0n1  joins node_disk_* / node_disk_info
  homelab_smart_disk_label  device=nvme0    joins smartctl_device*

That is what lets a panel replace a nested label_replace chain with one
group_left. They are the same disk; SATA devices render identically in both.

No `host` label: the scrape config attaches one, and emitting our own would only
produce a redundant `exported_host` (see homelab_zpool_* in VictoriaMetrics).
Names therefore only have to be unique *within* a host, which they are by
construction -- a disk belongs to at most one pool at one position. Grouping by
(host, disk_label) rather than disk_label alone is also what makes a cross-host
collision structurally impossible instead of merely test-enforced.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

OUT_DIR = Path(os.environ.get("TEXTFILE_DIR", "/var/lib/prometheus/node-exporter"))
OUT_FILE = OUT_DIR / "disk-labels.prom"

SYS_BLOCK = Path(os.environ.get("SYS_BLOCK_DIR", "/sys/block"))
# Optional model -> component-name overrides, for the rare disk whose useful
# name is not derivable (in no pool and not the boot disk). Keyed by model,
# never by serial: a model is not a hardware identifier, so this file stays safe
# to render from the public repo.
OVERRIDES_FILE = Path(os.environ.get("DISK_LABEL_OVERRIDES_FILE", "/etc/homelab/disk-labels.conf"))

# Only whole disks that node_exporter's diskstats collector reports and the
# Grafana panels filter on. Partitions, zram, loop and device-mapper nodes are
# not physical disks and would double-count.
DEVICE_PATTERN = re.compile(r"^(sd[a-z]+|nvme\d+n\d+)$")

# What an unpooled boot disk is called. Lowercase like every pool name, so a
# legend never mixes casing within one host's disks.
ROOT_COMPONENT = "boot"

# Transient container vdevs whose children are two views of one physical slot:
# the disk on its way out and the disk taking its place. Numbering their
# children separately would invent a member the pool does not have and shift
# every later member by one for the duration of the resilver.
SLOT_GROUP_PATTERN = re.compile(r"^(replacing|spare)-\d+\b")


def read_sysfs(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def smart_device(device: str) -> str:
    """The name smartctl_exporter uses for the same disk.

    smartctl probes the NVMe controller (nvme0), node_exporter reports the
    namespace block device (nvme0n1). SATA disks are named identically by both.
    """
    match = re.match(r"^(nvme\d+)n\d+$", device)
    return match.group(1) if match else device


def match_override(model: str, overrides: dict[str, str]) -> str:
    """Look up a model override, allowing the key to be a prefix of the model.

    The model here is the SCSI/NVMe INQUIRY string from
    /sys/block/<dev>/device/model, which for a USB enclosure carries a firmware
    revision on the end ("My Passport 0837"). Matching on a prefix means the
    override survives a firmware bump, and keeps the key readable. Exact matches
    win, and longer prefixes beat shorter ones so a more specific rule cannot be
    shadowed by a general one.
    """
    if not model:
        return ""
    if model in overrides:
        return overrides[model]
    candidates = [key for key in overrides if model.startswith(key)]
    return overrides[max(candidates, key=len)] if candidates else ""


def load_overrides() -> dict[str, str]:
    """model -> component name, from an optional `model = Name` config file."""
    overrides: dict[str, str] = {}
    try:
        lines = OVERRIDES_FILE.read_text().splitlines()
    except OSError:
        return overrides
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        model, name = line.split("=", 1)
        model, name = model.strip(), name.strip()
        if model and name:
            overrides[model] = name
    return overrides


def discover_disks() -> dict[str, dict[str, str]]:
    """Every whole physical disk, keyed by kernel device name."""
    disks: dict[str, dict[str, str]] = {}
    if not SYS_BLOCK.is_dir():
        return disks
    for entry in sorted(SYS_BLOCK.iterdir()):
        if not DEVICE_PATTERN.match(entry.name):
            continue
        # Virtual block devices have no backing `device` node. A whole disk
        # always does, so this also filters out anything synthesised.
        if not (entry / "device").exists():
            continue
        sectors = read_sysfs(entry / "size")
        disks[entry.name] = {
            "device": entry.name,
            "smart_device": smart_device(entry.name),
            # Exact bytes, published as-is. node_exporter has no whole-disk size
            # metric, so this is the only source -- and keeping it a raw number
            # means no rounding decision is baked into anything.
            # /sys/block/*/size is in 512-byte sectors regardless of the disk's
            # real logical block size; that is the kernel's fixed unit here.
            "size_bytes": str(int(sectors) * 512) if sectors.isdigit() else "",
            "rotational": read_sysfs(entry / "queue" / "rotational") or "0",
            "model": read_sysfs(entry / "device" / "model"),
            "pool": "",
            "position": "",
        }
    return disks


def leaf_to_device(path: str) -> str:
    """A `zpool status -LP` member path -> the whole-disk kernel name, or "".

    -L resolves a member to its kernel name, but only if the device is present:
    an OFFLINE member keeps whatever path it was added by (typically
    /dev/disk/by-id/...), which resolves to nothing. Returning "" for those is
    correct -- there is no block device, so there are no metrics to label --
    while the caller still counts it for position numbering, so removing a
    failed disk does not renumber its healthy siblings.
    """
    name = os.path.basename(path)
    # sda1 -> sda, nvme0n1p3 -> nvme0n1
    name = re.sub(r"p\d+$", "", name) if re.match(r"^nvme\d+n\d+p\d+$", name) else name
    name = re.sub(r"\d+$", "", name) if re.match(r"^sd[a-z]+\d+$", name) else name
    return name if DEVICE_PATTERN.match(name) else ""


def parse_zpool_status(output: str) -> dict[str, dict[str, str]]:
    """device -> {pool, position} from `zpool status -LP` config blocks.

    Position is the member's ordinal within its pool, in the order zpool prints
    it -- which is vdev order, not device order (clovis's vault-hdd starts at
    sde). A pool with a single member gets no position, because there is nothing
    to distinguish it from.

    Members are found by looking for a leading "/" rather than by measuring
    indentation. That is deliberate: this used to track container vdevs and
    their depth so it could tell a raidz from a mirror and number them
    differently, and all of that existed only to put "Z1" and "A"/"B" in a name.
    Dropping that from the name dropped the parse with it.

    Two consequences, both acceptable here and neither reachable in this fleet:
    a pool with several vdevs numbers straight through them rather than
    restarting per vdev, and a cache/log/spare device would be numbered inline
    with the data members.

    The one container vdev that is tracked is `replacing-N`/`spare-N`, because
    its children are not separate members: they are the outgoing and incoming
    disk for a single slot. Both are numbered with that one slot, so a
    replacement in progress does not report a member the pool does not have.
    Indentation is only consulted to find where such a group ends.
    """
    # pool -> list of slots, each slot holding the devices that share it (one,
    # except while a replacing-/spare- group is in flight).
    order: dict[str, list[list[str]]] = {}
    pool = ""
    in_config = False
    group_indent: int | None = None

    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("pool:"):
            pool, in_config, group_indent = line.split(":", 1)[1].strip(), False, None
            continue
        if line.startswith("config:"):
            in_config, group_indent = True, None
            continue
        if not (in_config and raw.startswith("\t")):
            continue

        indent = len(raw) - len(raw.lstrip())
        if group_indent is not None and indent <= group_indent:
            group_indent = None

        if SLOT_GROUP_PATTERN.match(line):
            order.setdefault(pool, []).append([])
            group_indent = indent
            continue
        if not line.startswith("/"):
            continue

        # Count unresolvable members too, so a failed disk does not renumber
        # the ones that are still there.
        device = leaf_to_device(line.split()[0])
        slots = order.setdefault(pool, [])
        if group_indent is not None and slots:
            slots[-1].append(device)
        else:
            slots.append([device])

    return {
        device: {"pool": pool, "position": f"D{index + 1}" if len(slots) > 1 else ""}
        for pool, slots in order.items()
        for index, slot in enumerate(slots)
        for device in slot
        if device
    }


def zpool_membership() -> dict[str, dict[str, str]]:
    try:
        result = subprocess.run(
            ["zpool", "status", "-LP"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # A host with no ZFS is legitimate (every disk then falls back to
        # root-filesystem detection), so this is not fatal.
        print(f"zpool status failed: {exc}", file=sys.stderr)
        return {}
    if result.returncode != 0:
        return {}
    return parse_zpool_status(result.stdout)


def root_device() -> str:
    """The whole disk backing /, when / is not on ZFS.

    cinci and cottonwood boot from a plain ext4 root, so their boot disk has no
    pool to be named after; without this it would fall through to being called
    by its kernel device name.
    """
    try:
        result = subprocess.run(
            ["findmnt", "-no", "SOURCE", "/"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    source = result.stdout.strip()
    return leaf_to_device(source) if source.startswith("/dev/") else ""


def component(disk: dict[str, str], overrides: dict[str, str]) -> str:
    """What this disk is, within its host. The host itself is not part of it."""
    override = match_override(disk["model"], overrides)
    if override:
        return override
    if disk["pool"]:
        return disk["pool"]
    if disk.get("is_root"):
        return ROOT_COMPONENT
    return disk["device"]


def compose_label(disk: dict[str, str], overrides: dict[str, str]) -> str:
    parts = [component(disk, overrides)]
    if disk["position"]:
        parts.append(disk["position"])
    return " ".join(parts)


def build(
    disks: dict[str, dict[str, str]],
    membership: dict[str, dict[str, str]],
    root: str,
    overrides: dict[str, str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for device in sorted(disks):
        disk = dict(disks[device])
        disk.update(membership.get(device, {}))
        disk["is_root"] = "1" if device == root and not disk["pool"] else ""
        disk["disk_label"] = compose_label(disk, overrides)
        rows.append(disk)
    return rows


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


def labels(disk: dict[str, str], device: str) -> str:
    pairs = {
        "device": device,
        "disk_label": disk["disk_label"],
        "pool": disk["pool"],
        "position": disk["position"],
        "rotational": disk["rotational"],
        "model": disk["model"],
        "size_bytes": disk["size_bytes"],
    }
    return ",".join(f'{key}="{escape(value)}"' for key, value in pairs.items() if value)


def render(rows: list[dict[str, str]]) -> str:
    lines = [
        "# HELP homelab_disk_label Human-readable name for a physical disk within its host, "
        "derived from its ZFS pool and position in that pool. Join on (host, device) with "
        "node_disk_*, group by (host, disk_label) and render '{{host}} {{disk_label}}'. "
        "Value is always 1.",
        "# TYPE homelab_disk_label gauge",
        "# HELP homelab_smart_disk_label The same name keyed by the device as "
        "smartctl_exporter names it (nvme0 rather than nvme0n1), for joining on "
        "(host, device) with smartctl_device*. Value is always 1.",
        "# TYPE homelab_smart_disk_label gauge",
    ]
    for disk in rows:
        lines.append(f"homelab_disk_label{{{labels(disk, disk['device'])}}} 1")
    # Two namespaces on one NVMe controller (nvme0n1, nvme0n2) collapse to the
    # same smartctl device, and node_exporter rejects the *entire* textfile on a
    # duplicate metric -- which would unlabel every disk on the host rather than
    # just the ambiguous one. Keep the first and say why the others are missing.
    seen: set[str] = set()
    for disk in rows:
        device = disk["smart_device"]
        if device in seen:
            print(
                f"{disk['device']}: shares smartctl device {device} with an earlier "
                "namespace; omitting its homelab_smart_disk_label series",
                file=sys.stderr,
            )
            continue
        seen.add(device)
        lines.append(f"homelab_smart_disk_label{{{labels(disk, device)}}} 1")
    return "\n".join(lines) + "\n"


def write_out(content: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=OUT_DIR, prefix=".disk-labels.prom.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, OUT_FILE)
    except BaseException:
        os.unlink(tmp_path)
        raise


def main() -> int:
    disks = discover_disks()
    if not disks:
        # Bare metal always has at least a boot disk, and this exporter is only
        # deployed to bare metal. Finding none means sysfs is not what we think
        # it is; fail loudly (SystemdUnitFailed already alerts on it) rather
        # than publishing an empty file that silently unlabels every panel.
        print("no physical disks found under /sys/block", file=sys.stderr)
        return 1
    rows = build(disks, zpool_membership(), root_device(), load_overrides())
    write_out(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
