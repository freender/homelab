#!/usr/bin/env python3
"""Write Broadcom/LSI SAS HBA controller temperatures for the node_exporter
textfile collector.

The SAS2308/SAS3008 ASIC has an on-die temperature sensor, but the mpt3sas
driver does not surface it through hwmon or sysfs -- `sensors` shows the CPU,
NVMe and NIC on these hosts and nothing for the HBA. The value is only reachable
by asking the firmware for MPI2 IO Unit Page 7, which is what storcli (Linux) and
mpsutil (FreeBSD) do. The driver exposes that path to userspace as a
message-passing character device (/dev/mpt2ctl for SAS2 hardware, /dev/mpt3ctl
for SAS3), so this exporter issues the same two-step CONFIG read:

  1. Action=PAGE_HEADER  -- firmware reports the page length
  2. Action=READ_CURRENT -- firmware DMAs the page into our buffer

Both are read-only config-page reads; the kernel driver owns the DMA buffers and
the SGE, so nothing here touches hardware registers directly. Constants and
struct layouts come from the kernel tree:

  drivers/scsi/mpt3sas/mpt3sas_ctl.h    mpt3_ioctl_command, MPT3COMMAND
  drivers/scsi/mpt3sas/mpi/mpi2_cnfg.h  Mpi2ConfigRequest_t, Mpi2IOUnitPage7_t

The sensor updates slowly (firmware samples it on its own interval), so polling
faster than once a minute buys nothing.

Controllers are discovered from sysfs rather than by probing IOC numbers blind:
scsi_host attributes give the board name, chip, firmware revision and PCI
address for labelling, and mpt3sas sets `unique_id` to the IOC number the ioctl
interface expects.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
import sys
import tempfile
from pathlib import Path

OUT_DIR = Path(os.environ.get("TEXTFILE_DIR", "/var/lib/prometheus/node-exporter"))
OUT_FILE = OUT_DIR / "hba-temp.prom"

SCSI_HOST_DIR = Path("/sys/class/scsi_host")
MPT_DRIVERS = {"mpt2sas": "/dev/mpt2ctl", "mpt3sas": "/dev/mpt3ctl"}

# _IOWR() from include/uapi/asm-generic/ioctl.h
_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_READ_WRITE = 3


def _iowr(type_: int, nr: int, size: int) -> int:
    return (
        (_IOC_READ_WRITE << _IOC_DIRSHIFT)
        | (type_ << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


MPT3_MAGIC = ord("L")
# sizeof(struct mpt3_ioctl_command) on x86-64; the ioctl number encodes it, so a
# mismatch here is rejected by the driver rather than silently misparsed.
SIZEOF_COMMAND = 72
# offsetof(struct mpt3_ioctl_command, mf) -- the message frame follows the fixed
# fields and is copied separately by the driver.
MF_OFFSET = 68
MPT3COMMAND = _iowr(MPT3_MAGIC, 20, SIZEOF_COMMAND)

MPI2_FUNCTION_CONFIG = 0x04
MPI2_CONFIG_PAGETYPE_IO_UNIT = 0x00
ACTION_PAGE_HEADER = 0x00
ACTION_READ_CURRENT = 0x01
IOUNIT_PAGE_NUMBER = 7
IOCSTATUS_MASK = 0x7FFF
# The page buffer SGE starts at byte 28 of the CONFIG request; the driver builds
# it itself and only copies the preceding 7 dwords from us.
SGE_DWORD_OFFSET = 7
REPLY_SIZE = 256
IOCTL_TIMEOUT_SEC = 30

# Mpi2IOUnitPage7_t field offsets.
OFF_IOC_TEMPERATURE = 0x10
OFF_BOARD_TEMPERATURE = 0x14

TEMP_UNITS_NOT_PRESENT = 0x00
TEMP_UNITS_FAHRENHEIT = 0x01
TEMP_UNITS_CELSIUS = 0x02


class HbaError(Exception):
    """A controller was found but its temperature could not be read."""


def read_sysfs(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def discover_controllers() -> list[dict[str, str]]:
    """Every mpt2sas/mpt3sas controller, with its labels and IOC number."""
    controllers: list[dict[str, str]] = []
    if not SCSI_HOST_DIR.is_dir():
        return controllers

    for host_dir in sorted(SCSI_HOST_DIR.iterdir()):
        driver = read_sysfs(host_dir / "proc_name")
        if driver not in MPT_DRIVERS:
            continue
        unique_id = read_sysfs(host_dir / "unique_id")
        if not unique_id.isdigit():
            continue
        # /sys/class/scsi_host/hostN resolves to
        # .../<pci-address>/hostN/scsi_host/hostN, so the PCI address is three
        # levels up. It is the only controller identity that survives a reboot;
        # scsi host numbering does not.
        resolved = host_dir.resolve()
        pci = resolved.parents[2].name if len(resolved.parents) >= 3 else ""
        controllers.append(
            {
                "driver": driver,
                "ioc": unique_id,
                "pci": pci,
                "board": read_sysfs(host_dir / "board_name") or "unknown",
                "chip": read_sysfs(host_dir / "version_product") or "unknown",
                "firmware": read_sysfs(host_dir / "version_fw"),
            }
        )
    return controllers


def config_request(
    fd: int, ioc: int, action: int, header: bytes, data_len: int
) -> tuple[int, bytes, bytes]:
    """Issue one MPI2 CONFIG request; returns (IOCStatus, reply header, page)."""
    reply = ctypes.create_string_buffer(REPLY_SIZE)
    data = ctypes.create_string_buffer(data_len) if data_len else None

    cmd = ctypes.create_string_buffer(MF_OFFSET + 28)
    struct.pack_into("<I", cmd, 0, ioc)  # hdr.ioc_number
    struct.pack_into("<I", cmd, 12, IOCTL_TIMEOUT_SEC)
    struct.pack_into("<Q", cmd, 16, ctypes.addressof(reply))  # reply_frame_buf_ptr
    struct.pack_into("<Q", cmd, 24, ctypes.addressof(data) if data else 0)  # data_in_buf_ptr
    struct.pack_into("<I", cmd, 48, REPLY_SIZE)  # max_reply_bytes
    struct.pack_into("<I", cmd, 52, data_len)  # data_in_size
    struct.pack_into("<I", cmd, 64, SGE_DWORD_OFFSET)

    # Mpi2ConfigRequest_t, laid out in the message frame.
    struct.pack_into("<B", cmd, MF_OFFSET + 0, action)
    struct.pack_into("<B", cmd, MF_OFFSET + 3, MPI2_FUNCTION_CONFIG)
    cmd[MF_OFFSET + 20 : MF_OFFSET + 24] = header  # Mpi2ConfigPageHeader_t

    fcntl.ioctl(fd, MPT3COMMAND, cmd)

    ioc_status = struct.unpack_from("<H", reply, 0x0E)[0] & IOCSTATUS_MASK
    return ioc_status, reply.raw[0x14:0x18], (data.raw if data else b"")


def read_iounit_page7(fd: int, ioc: int) -> bytes:
    header = bytes([0, 0, IOUNIT_PAGE_NUMBER, MPI2_CONFIG_PAGETYPE_IO_UNIT])
    status, reply_header, _ = config_request(fd, ioc, ACTION_PAGE_HEADER, header, 0)
    if status:
        raise HbaError(f"PAGE_HEADER returned IOCStatus 0x{status:04x}")

    page_len_dwords = reply_header[1]
    if page_len_dwords == 0:
        raise HbaError("firmware reports IO Unit Page 7 length 0 (page unsupported)")
    page_bytes = page_len_dwords * 4
    if page_bytes < OFF_IOC_TEMPERATURE + 3:
        raise HbaError(f"IO Unit Page 7 is only {page_bytes} bytes; no temperature field")

    # The header the firmware returned carries the page version and length it
    # expects back, so echo it rather than the one we asked with.
    status, _, page = config_request(fd, ioc, ACTION_READ_CURRENT, reply_header, page_bytes)
    if status:
        raise HbaError(f"READ_CURRENT returned IOCStatus 0x{status:04x}")
    return page


def to_celsius(raw: int, units: int) -> float | None:
    if units == TEMP_UNITS_CELSIUS:
        return float(raw)
    if units == TEMP_UNITS_FAHRENHEIT:
        return (raw - 32) * 5.0 / 9.0
    return None


def read_temperatures(controller: dict[str, str]) -> dict[str, float]:
    """{sensor: celsius} for one controller. Raises HbaError on any failure."""
    device = MPT_DRIVERS[controller["driver"]]
    try:
        fd = os.open(device, os.O_RDWR)
    except OSError as exc:
        raise HbaError(f"cannot open {device}: {exc}") from exc
    try:
        page = read_iounit_page7(fd, int(controller["ioc"]))
    except OSError as exc:
        raise HbaError(f"ioctl on {device} failed: {exc}") from exc
    finally:
        os.close(fd)

    temps: dict[str, float] = {}
    ioc_raw, ioc_units = struct.unpack_from("<HB", page, OFF_IOC_TEMPERATURE)
    ioc_temp = to_celsius(ioc_raw, ioc_units)
    if ioc_temp is None:
        # Some OEM rebadges omit the sensor entirely; distinguish that from a
        # failed read so it does not look like a broken exporter.
        raise HbaError("firmware reports no IOC temperature sensor")
    temps["ioc"] = ioc_temp

    # Optional second sensor; absent on the 9207-8i but present on some boards.
    if len(page) >= OFF_BOARD_TEMPERATURE + 3:
        board_raw, board_units = struct.unpack_from("<HB", page, OFF_BOARD_TEMPERATURE)
        if board_units != TEMP_UNITS_NOT_PRESENT:
            board_temp = to_celsius(board_raw, board_units)
            if board_temp is not None:
                temps["board"] = board_temp
    return temps


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


def labels(controller: dict[str, str], **extra: str) -> str:
    pairs = {
        "pci": controller["pci"],
        "board": controller["board"],
        "chip": controller["chip"],
        **extra,
    }
    # No host label: the scrape config attaches one, and emitting our own only
    # produces a redundant exported_host (see homelab_zpool_* in VictoriaMetrics).
    return ",".join(f'{key}="{escape(value)}"' for key, value in pairs.items())


def render(controllers: list[dict[str, str]]) -> str:
    lines = [
        "# HELP homelab_hba_temperature_celsius SAS HBA temperature in degrees Celsius, "
        "read from MPI2 IO Unit Page 7 (sensor=ioc is the controller ASIC die).",
        "# TYPE homelab_hba_temperature_celsius gauge",
        "# HELP homelab_hba_temperature_read_success Whether the last IO Unit Page 7 read "
        "for this controller succeeded (1) or failed (0).",
        "# TYPE homelab_hba_temperature_read_success gauge",
        "# HELP homelab_hba_info SAS HBA identity; value is always 1, detail is in labels.",
        "# TYPE homelab_hba_info gauge",
    ]
    for controller in controllers:
        info_labels = labels(
            controller,
            driver=controller["driver"],
            firmware=controller["firmware"],
        )
        lines.append(f"homelab_hba_info{{{info_labels}}} 1")
        try:
            temps = read_temperatures(controller)
        except HbaError as exc:
            print(f"{controller['pci']}: {exc}", file=sys.stderr)
            lines.append(f"homelab_hba_temperature_read_success{{{labels(controller)}}} 0")
            continue
        lines.append(f"homelab_hba_temperature_read_success{{{labels(controller)}}} 1")
        for sensor, celsius in temps.items():
            temp_labels = labels(controller, sensor=sensor)
            lines.append(f"homelab_hba_temperature_celsius{{{temp_labels}}} {celsius:g}")
    return "\n".join(lines) + "\n"


def main() -> int:
    controllers = discover_controllers()
    if not controllers:
        # This exporter is only deployed to hosts that have an HBA, so finding
        # none means the card or its driver is gone. Fail loudly (the unit goes
        # failed, which SystemdUnitFailed already alerts on) instead of quietly
        # publishing an empty file.
        print("no mpt2sas/mpt3sas controllers found", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    content = render(controllers)
    fd, tmp_path = tempfile.mkstemp(dir=OUT_DIR, prefix=".hba-temp.prom.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, OUT_FILE)
    except BaseException:
        os.unlink(tmp_path)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
