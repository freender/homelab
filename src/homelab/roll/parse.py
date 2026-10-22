"""Parse what the roll survey reads from a host. Pure: text in, values out.

Every reader returns None (or an empty collection) for output it cannot make sense
of, never a guessed default: the checks turn "no reading" into a stop condition of
its own, because a missing answer must not read as a healthy one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

SECTION_MARK = "@@"
POOLS_HEALTHY = "all pools are healthy"

PROM_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)$")
LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
HA_RE = re.compile(r"^(lrm|service) (\S+) \(([^)]*)\)")
APT_RE = re.compile(r"(\d+) upgraded, (\d+) newly installed, (\d+) to remove")
INST_RE = re.compile(r"^Inst (\S+)")
# `-N` after the version on PVE, Ubuntu and Debian <= 12; `+debN` on Debian 13.
KERNEL_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+[-+][0-9a-z]")
SYNC_FORMAT = "%Y-%m-%d_%H:%M:%S"


def sections(text: str) -> dict[str, str]:
    """Split survey-script output into its `@@name` sections."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith(SECTION_MARK):
            current = found.setdefault(line[len(SECTION_MARK) :].strip(), [])
        elif current is not None:
            current.append(line)
    return {name: "\n".join(lines).strip() for name, lines in found.items()}


def to_int(text: str | None) -> int | None:
    try:
        return int(str(text).strip())
    except ValueError:
        return None


def to_float(text: str | None) -> float | None:
    try:
        return float(str(text).strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float


def prom_sample(text: str, name: str) -> Sample | None:
    """The first sample of one metric in a node_exporter textfile."""
    for line in text.splitlines():
        match = PROM_RE.match(line.strip())
        if match and match.group(1) == name:
            value = to_float(match.group(3))
            if value is None:
                return None
            return Sample(dict(LABEL_RE.findall(match.group(2) or "")), value)
    return None


@dataclass(frozen=True)
class Kernel:
    # None: the reboot exporter has no reading, which is its own failure.
    required: bool | None
    running: str
    installed: str


def parse_kernel(text: str) -> Kernel:
    info = prom_sample(text, "homelab_kernel_info")
    flag = prom_sample(text, "homelab_reboot_required")
    labels = info.labels if info else {}
    return Kernel(
        required=None if flag is None else flag.value == 1,
        running=labels.get("running", ""),
        installed=labels.get("installed", ""),
    )


@dataclass(frozen=True)
class Boot:
    dead: int | None
    first_loadable: bool | None


def parse_boot(text: str) -> Boot:
    dead = prom_sample(text, "homelab_boot_entries_dead")
    loadable = prom_sample(text, "homelab_boot_first_entry_loadable")
    return Boot(
        dead=None if dead is None else int(dead.value),
        first_loadable=None if loadable is None else loadable.value == 1,
    )


def colon_fields(text: str) -> dict[str, str]:
    """`Key:   value` lines, as printed by pvecm and apcaccess."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields.setdefault(key.strip(), value.strip())
    return fields


@dataclass(frozen=True)
class Quorum:
    quorate: bool
    total: int | None
    expected: int | None


def parse_quorum(text: str) -> Quorum:
    fields = colon_fields(text)
    return Quorum(
        quorate=fields.get("Quorate") == "Yes",
        total=to_int(fields.get("Total votes")),
        expected=to_int(fields.get("Expected votes")),
    )


@dataclass(frozen=True)
class HaService:
    sid: str
    node: str
    state: str


@dataclass(frozen=True)
class HaStatus:
    quorum_ok: bool
    lrms: dict[str, str]
    services: dict[str, HaService]


def parse_ha(text: str) -> HaStatus:
    """`ha-manager status`. An LRM's state is the first field in its parentheses."""
    lrms: dict[str, str] = {}
    services: dict[str, HaService] = {}
    for line in text.splitlines():
        match = HA_RE.match(line.strip())
        if not match:
            continue
        kind, name, inside = match.groups()
        parts = [part.strip() for part in inside.split(",")]
        if kind == "lrm":
            lrms[name] = parts[0]
        else:
            services[name] = HaService(name, parts[0], parts[1] if len(parts) > 1 else "")
    quorum_ok = any(line.strip() == "quorum OK" for line in text.splitlines())
    return HaStatus(quorum_ok, lrms, services)


@dataclass(frozen=True)
class ReplJob:
    job: str
    enabled: bool
    target: str
    last_sync: datetime | None
    fail_count: int
    state: str


def sync_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), SYNC_FORMAT)
    except ValueError:
        return None


def parse_replication(text: str) -> list[ReplJob]:
    """`pvesr status`: whitespace columns, the State column last and free text."""
    jobs: list[ReplJob] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 8 or parts[0] == "JobID":
            continue
        jobs.append(
            ReplJob(
                job=parts[0],
                enabled=parts[1] == "Yes",
                target=parts[2],
                last_sync=sync_time(parts[3]),
                fail_count=to_int(parts[6]) or 0,
                state=" ".join(parts[7:]),
            )
        )
    return jobs


def json_list(text: str) -> list | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def task_names(text: str) -> list[str] | None:
    """Running PVE or PBS tasks, as `type id`; None when the list is unreadable."""
    tasks = json_list(text)
    if tasks is None:
        return None
    return [_task_name(task) for task in tasks if isinstance(task, dict)]


def _task_name(task: dict) -> str:
    kind = task.get("type") or task.get("worker_type") or "task"
    ident = task.get("id") or task.get("worker_id") or ""
    return f"{kind} {ident}".strip()


def properties(text: str) -> dict[str, str]:
    """`systemctl show -p ...` output."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found[key.strip()] = value.strip()
    return found


def apt_pending(text: str) -> int | None:
    """Packages an `apt-get -s dist-upgrade` would still change."""
    match = APT_RE.search(text)
    if match is None:
        return None
    return sum(int(group) for group in match.groups())


def kernel_release(package: str) -> str | None:
    """The release a kernel package boots, by the reboot exporter's own rule.

    Meta and helper packages (linux-image-amd64, proxmox-kernel-helper,
    proxmox-kernel-7.0) carry no release and return None, as do debug packages.
    """
    if package.endswith(("-dbg", "-dbgsym")):
        return None
    release = package
    for prefix in ("linux-image-", "unsigned-", "proxmox-kernel-"):
        release = release.removeprefix(prefix)
    for suffix in ("-signed", "-unsigned"):
        release = release.removesuffix(suffix)
    if release == package or not KERNEL_RE.match(release):
        return None
    return release


@dataclass(frozen=True)
class AptChanges:
    total: int
    # Kernel releases the upgrade would install.
    kernels: list[str]


def apt_changes(text: str) -> AptChanges | None:
    """`apt-get -s dist-upgrade` filtered to its `Inst` lines and summary."""
    total = apt_pending(text)
    if total is None:
        return None
    packages = [match.group(1) for match in map(INST_RE.match, text.splitlines()) if match]
    kernels = sorted({release for release in map(kernel_release, packages) if release})
    return AptChanges(total, kernels)


def tab_rows(text: str, width: int) -> list[list[str]]:
    """Tab-separated rows of exactly `width` fields; anything else is noise."""
    rows = [line.split("\t") for line in text.splitlines()]
    return [[field.strip() for field in row] for row in rows if len(row) == width]


@dataclass(frozen=True)
class SwarmNode:
    hostname: str
    status: str
    manager: str
    is_self: bool


def parse_swarm(text: str) -> list[SwarmNode]:
    """`docker node ls --format '{{.Hostname}}\\t{{.Status}}\\t{{.ManagerStatus}}\\t{{.Self}}'`.

    Empty on a host that is not a swarm manager: there the command fails.
    """
    return [
        SwarmNode(host, status, manager, is_self == "true")
        for host, status, manager, is_self in tab_rows(text, 4)
    ]


def unit_names(text: str) -> list[str]:
    """First column of `systemctl --failed --no-legend --plain`."""
    return [line.split()[0] for line in text.splitlines() if line.split()]


def service_down(unit: dict[str, str]) -> bool:
    """An enabled service that should be running and is not.

    A oneshot finishes by design, a failed condition skips the unit on purpose
    (smartmontools on a VM with no SMART disk), and a socket-activated unit runs
    only on demand.
    """
    return (
        unit.get("ActiveState") != "active"
        and unit.get("Type") != "oneshot"
        and unit.get("ConditionResult") != "no"
        and not unit.get("TriggeredBy")
    )


def down_services(text: str) -> list[str] | None:
    """`systemctl show -p Id -p Type -p ActiveState -p ConditionResult -p TriggeredBy`
    over every enabled service: the ones that are down. None when nothing was
    listed, which on a system manager means the reading failed (ssh is enabled)."""
    units = [properties(block) for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not units:
        return None
    return sorted(unit.get("Id", "?") for unit in units if service_down(unit))


def onboot_guests(text: str) -> list[int] | None:
    """VMIDs of the configs under /etc/pve/local set to start on boot.

    The survey prints `listed` before the grep, so a node whose /etc/pve is not
    mounted reads as None, not as a node with no guests.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "listed":
        return None
    stems = [line.strip().rsplit("/", 1)[-1].removesuffix(".conf") for line in lines[1:]]
    return sorted(vmid for vmid in map(to_int, stems) if vmid is not None)


def unhealthy_containers(text: str) -> list[str]:
    """Containers (`docker ps -a`, `{{.Names}}\\t{{.Status}}`) that are not plainly up."""
    bad = []
    for name, status in tab_rows(text, 2):
        if not status.startswith("Up") or "(unhealthy)" in status or "health: starting" in status:
            bad.append(f"{name} ({status})")
    return bad


def up_values(text: str) -> list[float] | None:
    """The `up` samples a VictoriaMetrics instant query returned."""
    try:
        result = json.loads(text)["data"]["result"]
        return [float(series["value"][1]) for series in result]
    except (ValueError, KeyError, TypeError, IndexError):
        return None
