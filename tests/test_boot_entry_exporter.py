"""Behavioural tests for metrics-exporters' boot-entry-textfile-exporter.

Every fixture below is real `efibootmgr -v` output captured from a homelab node
on 2026-09-19/20, including the indented `dp:`/`data:` continuation lines that
the real tool emits and a parser must ignore. They are reproduced verbatim
rather than paraphrased because the incident that motivated this exporter came
down to one character of device-path shape.

The case that matters most is `test_network_boot_entries_are_not_dead`. The
first version of this check classified an entry as dead when it lacked a
HD()/File() component, which is true of the husks -- and equally true of every
PXE/HTTP boot entry, because those use MAC()/IPv4()/Uri() paths. Acting on that
rule would have deleted the PXE entries arc's auto-install delivery depends on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "boot-entry-textfile-exporter"

# ace, 2026-09-19, as found after the operator had already changed the boot
# entry in the BIOS to recover the host -- hence 0019 first. Boot0002/0003 are
# the two firmware-mangled "Linux Boot Manager" husks that made the BIOS menu
# ambiguous; 0000/0001 are equally dead leftovers from earlier installs.
ACE_AS_FOUND = """\
BootCurrent: 0019
Timeout: 1 seconds
BootOrder: 0019,0004,0013,0014,0015,0016,0017,0018,0002,0001,0000,0003
Boot0000* Windows Boot Manager\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)57494e444f5753
      dp: 01 04 14 00 e7 75 e2 99 a0 75 37 4b / 7f ff 04 00
    data: 57 49 4e 44 4f 57 53 00 01 00 00 00
Boot0001* proxmox\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
      dp: 01 04 14 00 e7 75 e2 99 a0 75 37 4b / 7f ff 04 00
Boot0002* Linux Boot Manager\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
      dp: 01 04 14 00 e7 75 e2 99 a0 75 37 4b / 7f ff 04 00
Boot0003* Linux Boot Manager\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
      dp: 01 04 14 00 e7 75 e2 99 a0 75 37 4b / 7f ff 04 00
Boot0004* Linux Boot Manager\tHD(2,GPT,767145b8-62ac-46a2-a05b-b479caf2463e,0x800,0x200000)/File(\\EFI\\SYSTEMD\\SYSTEMD-BOOTX64.EFI)
Boot0013  UEFI: HTTP IP4 HP Ethernet 10Gb 2-port 560SFP+ Adapter - NIC\tPciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)/MAC(38eaa791cc60,1)/IPv4(0.0.0.00.0.0.0,0,0)/Uri()0000424f
Boot0016  UEFI: PXE IP4 HP Ethernet 10Gb 2-port 560SFP+ Adapter - NIC\tPciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)/MAC(38eaa791cc60,1)/IPv4(0.0.0.00.0.0.0,0,0)0000424f
Boot0019* UEFI OS\tHD(2,GPT,767145b8-62ac-46a2-a05b-b479caf2463e,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)0000424f
"""

# The same host with BootOrder as it must have been when it failed to boot:
# a husk ahead of every loadable entry. Reconstructed, and labelled as such --
# by the time anyone could read NVRAM the operator had already changed it.
ACE_AS_IT_FAILED = ACE_AS_FOUND.replace(
    "BootOrder: 0019,0004,0013,0014,0015,0016,0017,0018,0002,0001,0000,0003",
    "BootOrder: 0002,0003,0019,0004,0013,0016",
)

# ace after cleanup, verified by a real reboot: husks deleted, one Linux Boot
# Manager left, PXE entries retained.
ACE_CLEANED = """\
BootCurrent: 0019
BootOrder: 0019,0004,0013,0016
Boot0004* Linux Boot Manager\tHD(2,GPT,767145b8-62ac-46a2-a05b-b479caf2463e,0x800,0x200000)/File(\\EFI\\SYSTEMD\\SYSTEMD-BOOTX64.EFI)
Boot0013  UEFI: HTTP IP4 HP Ethernet 10Gb 2-port 560SFP+ Adapter - NIC\tPciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)/MAC(38eaa791cc60,1)/IPv4(0.0.0.00.0.0.0,0,0)/Uri()0000424f
Boot0016  UEFI: PXE IP4 HP Ethernet 10Gb 2-port 560SFP+ Adapter - NIC\tPciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)/MAC(38eaa791cc60,1)/IPv4(0.0.0.00.0.0.0,0,0)0000424f
Boot0019* UEFI OS\tHD(2,GPT,767145b8-62ac-46a2-a05b-b479caf2463e,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)0000424f
"""

# clovis, 2026-09-20: four husks, yet booting perfectly, because BootOrder
# happens to list the one real entry first. This is the state the warning rule
# exists to catch -- the critical rule must stay silent here.
CLOVIS_AS_FOUND = """\
BootCurrent: 0004
BootOrder: 0004,0009,0001,0002,0000,0003
Boot0000* Windows Boot Manager\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
Boot0001* Windows Boot Manager\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
Boot0002* proxmox\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
Boot0003* Linux Boot Manager\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
Boot0004* Linux Boot Manager\tHD(2,GPT,cddd0b7e-b080-4ff4-bc3d-8ec64650a800,0x800,0x200000)/File(\\EFI\\SYSTEMD\\SYSTEMD-BOOTX64.EFI)
Boot0009* UEFI OS\tHD(2,GPT,cddd0b7e-b080-4ff4-bc3d-8ec64650a800,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)0000424f
"""

# osiris, 2026-09-20. The four "UEFI:<device class>" entries are firmware stubs
# with neither File() nor MAC() nor a VenHw() husk shape. They are healthy and
# must land in the unrecognised-but-loadable bucket, not the dead one.
OSIRIS_AS_FOUND = """\
BootCurrent: 0000
BootOrder: 0000,0007,0006,0001,0008,0009,000A
Boot0000* Linux Boot Manager\tHD(2,GPT,7f2eaa26-7ab0-4683-8c93-f3dd1fa3ed90,0x800,0x200000)/File(\\EFI\\systemd\\systemd-bootx64.efi)
Boot0001* UEFI: Built-in EFI Shell\tVenMedia(5023b95c-db26-429b-a648-bd47664c8012)
Boot0006* UEFI: PXE IPv4 Intel(R) Ethernet Controller\tPciRoot(0x0)/Pci(0x1c,0x4)/MAC(3cecef4a1b2c,0)/IPv4(0.0.0.00.0.0.0,0,0)
Boot0007* UEFI OS\tHD(2,GPT,7f2eaa26-7ab0-4683-8c93-f3dd1fa3ed90,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)
Boot0008* UEFI:CD/DVD Drive\tBBS(129,,0x0)
Boot0009* UEFI:Removable Device\tBBS(130,,0x0)
Boot000A* UEFI:Network Device\tBBS(131,,0x0)
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def run_exporter(
    tmp_path: Path,
    *,
    efibootmgr_output: str | None = None,
    efibootmgr_rc: int = 0,
    efibootmgr_missing: bool = False,
    container: bool = False,
    uefi: bool = True,
    preexisting_prom: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    out_dir = tmp_path / "textfile"
    out_dir.mkdir(exist_ok=True)

    prom = out_dir / "boot-entries.prom"
    if preexisting_prom:
        prom.write_text("stale content\n", encoding="utf-8")

    efi_dir = tmp_path / "efi"
    if uefi:
        efi_dir.mkdir(exist_ok=True)

    _write_exec(stub_dir / "hostname", '#!/bin/bash\nprintf "%s\\n" "testhost"\n')
    _write_exec(
        stub_dir / "systemd-detect-virt",
        f"#!/bin/bash\nexit {0 if container else 1}\n",
    )

    efibootmgr_path = stub_dir / "efibootmgr"
    if not efibootmgr_missing:
        (tmp_path / "efi.out").write_text(efibootmgr_output or "", encoding="utf-8")
        _write_exec(
            efibootmgr_path,
            f'#!/bin/bash\ncat "{tmp_path}/efi.out"\nexit {efibootmgr_rc}\n',
        )

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["TEXTFILE_DIR"] = str(out_dir)
    env["EFI_SYS_DIR"] = str(efi_dir)
    env["EFIBOOTMGR"] = str(efibootmgr_path)

    result = subprocess.run(
        ["bash", str(EXPORTER)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, prom


def metric_value(text: str, name: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(f"{name}{{"):
            return line.rsplit(" ", 1)[1]
    return None


def dead_bootnums(text: str) -> set[str]:
    found = set()
    for line in text.splitlines():
        if line.startswith("homelab_boot_entry_dead{"):
            labels = line[line.index("{") + 1 : line.index("}")]
            for part in labels.split(","):
                key, _, value = part.partition("=")
                if key == "bootnum":
                    found.add(value.strip('"'))
    return found


def test_ace_as_it_failed_reports_unloadable_first_entry(tmp_path: Path) -> None:
    """The incident case: a husk ahead of every loadable entry in BootOrder."""
    result, prom = run_exporter(tmp_path, efibootmgr_output=ACE_AS_IT_FAILED)
    assert result.returncode == 0, result.stderr
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_boot_first_entry_loadable") == "0"
    assert metric_value(text, "homelab_boot_entries_dead") == "4"
    assert dead_bootnums(text) == {"0000", "0001", "0002", "0003"}


def test_ace_as_found_boots_but_still_reports_husks(tmp_path: Path) -> None:
    """Operator had already worked around it in the BIOS: boots, still dirty."""
    _, prom = run_exporter(tmp_path, efibootmgr_output=ACE_AS_FOUND)
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_boot_first_entry_loadable") == "1"
    assert metric_value(text, "homelab_boot_entries_dead") == "4"


def test_ace_cleaned_is_all_clear(tmp_path: Path) -> None:
    _, prom = run_exporter(tmp_path, efibootmgr_output=ACE_CLEANED)
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_boot_first_entry_loadable") == "1"
    assert metric_value(text, "homelab_boot_entries_dead") == "0"
    assert dead_bootnums(text) == set()


def test_clovis_warns_without_firing_the_critical(tmp_path: Path) -> None:
    """Husks present but a real entry first: warning yes, critical no.

    This asymmetry is the whole reason both metrics exist. A single combined
    signal would either miss clovis entirely or cry wolf about a host that
    boots fine.
    """
    _, prom = run_exporter(tmp_path, efibootmgr_output=CLOVIS_AS_FOUND)
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_boot_first_entry_loadable") == "1"
    assert metric_value(text, "homelab_boot_entries_dead") == "4"
    assert dead_bootnums(text) == {"0000", "0001", "0002", "0003"}


def test_network_boot_entries_are_not_dead(tmp_path: Path) -> None:
    """Regression: PXE/HTTP entries have no File() and must never be flagged.

    Deleting these would break arc's PXE/PDM auto-install delivery.
    """
    _, prom = run_exporter(tmp_path, efibootmgr_output=ACE_CLEANED)
    text = prom.read_text(encoding="utf-8")
    assert dead_bootnums(text) == set()
    assert metric_value(text, "homelab_boot_entries_dead") == "0"


def test_firmware_device_class_stubs_are_not_dead(tmp_path: Path) -> None:
    """osiris' BBS()/VenMedia() stubs are unrecognised, not husks."""
    _, prom = run_exporter(tmp_path, efibootmgr_output=OSIRIS_AS_FOUND)
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_boot_entries_dead") == "0"
    assert metric_value(text, "homelab_boot_first_entry_loadable") == "1"


def test_inactive_entries_are_ignored(tmp_path: Path) -> None:
    """An entry without the asterisk is skipped by firmware, so it cannot strand."""
    output = """\
BootOrder: 0001,0002
Boot0001  Dead But Inactive\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
Boot0002* Linux Boot Manager\tHD(2,GPT,abc,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)
"""
    _, prom = run_exporter(tmp_path, efibootmgr_output=output)
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_boot_entries_dead") == "0"
    assert metric_value(text, "homelab_boot_first_entry_loadable") == "1"


def test_bootorder_entry_with_no_matching_entry_is_skipped(tmp_path: Path) -> None:
    """Firmware silently skips a bootnum that no longer exists; so must we.

    Only a present-and-active-but-unloadable entry strands a host, which is
    the distinction that makes this metric mean anything.
    """
    output = """\
BootOrder: 00FF,0002
Boot0002* Linux Boot Manager\tHD(2,GPT,abc,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)
"""
    _, prom = run_exporter(tmp_path, efibootmgr_output=output)
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_boot_first_entry_loadable") == "1"


def test_missing_bootorder_omits_first_entry_metric(tmp_path: Path) -> None:
    """No BootOrder means the check cannot tell; it must not report a false 1."""
    output = """\
Boot0002* Linux Boot Manager\tHD(2,GPT,abc,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)
"""
    _, prom = run_exporter(tmp_path, efibootmgr_output=output)
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_boot_first_entry_loadable") is None
    assert metric_value(text, "homelab_boot_entries_dead") == "0"


def test_label_quotes_and_backslashes_are_escaped(tmp_path: Path) -> None:
    """Entry names are firmware-supplied free text, not a trusted label source."""
    output = """\
BootOrder: 0001
Boot0001* We\\ird "Name"\tVenHw(99e275e7-75a0-4b37-a2e6-c5385e6c00cb)
"""
    _, prom = run_exporter(tmp_path, efibootmgr_output=output)
    text = prom.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("homelab_boot_entry_dead{"))
    assert 'label="We\\\\ird \\"Name\\""' in line


def test_container_guest_writes_nothing_and_clears_stale(tmp_path: Path) -> None:
    result, prom = run_exporter(
        tmp_path, efibootmgr_output=ACE_CLEANED, container=True, preexisting_prom=True
    )
    assert result.returncode == 0
    assert not prom.exists()


def test_legacy_bios_writes_nothing_and_clears_stale(tmp_path: Path) -> None:
    """No /sys/firmware/efi means no NVRAM to inspect; absence is the signal."""
    result, prom = run_exporter(
        tmp_path, efibootmgr_output=ACE_CLEANED, uefi=False, preexisting_prom=True
    )
    assert result.returncode == 0
    assert not prom.exists()


def test_missing_efibootmgr_clears_stale(tmp_path: Path) -> None:
    result, prom = run_exporter(tmp_path, efibootmgr_missing=True, preexisting_prom=True)
    assert result.returncode == 0
    assert not prom.exists()


def test_efibootmgr_failure_clears_stale(tmp_path: Path) -> None:
    result, prom = run_exporter(
        tmp_path, efibootmgr_output="", efibootmgr_rc=1, preexisting_prom=True
    )
    assert result.returncode == 0
    assert not prom.exists()


def test_no_entries_refuses_to_report_all_clear(tmp_path: Path) -> None:
    """An empty enumeration is a broken check, not a healthy host."""
    result, prom = run_exporter(
        tmp_path, efibootmgr_output="BootOrder: 0001\n", preexisting_prom=True
    )
    assert result.returncode == 0
    assert not prom.exists()
