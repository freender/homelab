"""Behavioural tests for metrics-exporters' smartctl-exporter-wait-devices.

This script is an ExecStartPre: if it exits non-zero, or blocks past the unit's
TimeoutStartSec, smartctl_exporter does not start at all and the host loses
*every* SMART series. So the standing invariant is "always exit 0, eventually" --
the interesting axis is only how long it takes and what device list it settles
on.

`test_waits_past_a_long_still_period_for_a_remembered_disk` is the regression for
the incident this exists for: ace's device list held perfectly still for 64
seconds after boot before its LSI SAS2308 produced sda/sdb, so a plain
"unchanged for N seconds" check settles on the wrong list and reproduces the
HTTP 500. Only the remembered previous-boot list distinguishes the two cases.

Timings are scaled down via the HOMELAB_SMARTCTL_WAIT_* knobs so the suite stays
fast; the shipped defaults are pinned separately against the unit drop-in.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "metrics-exporters" / "configs" / "common" / "smartctl-exporter-wait-devices"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _scan_json(devices: list[str]) -> str:
    entries = ",\n".join(
        f'    {{"name": "{d}", "info_name": "{d}", "type": "scsi", "protocol": "SCSI"}}'
        for d in devices
    )
    return '{\n  "devices": [\n' + entries + "\n  ]\n}"


def run_wait(
    tmp_path: Path,
    *,
    scans: list[list[str]],
    remembered: list[str] | None = None,
    late_start: bool = False,
    smartctl: str | None = None,
    timeout: int = 3,
    settle: int = 1,
    poll: int = 1,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the script against a stub smartctl that walks `scans`, one per call.

    The last entry repeats forever, so a single-element `scans` is a device list
    that never changes. Returns the process result and the state-file path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    state = tmp_path / "state" / "smartctl-devices"
    counter = tmp_path / "calls"
    counter.write_text("0", encoding="utf-8")

    if remembered is not None:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(" ".join(sorted(remembered)) + "\n", encoding="utf-8")

    for index, devices in enumerate(scans):
        (tmp_path / f"scan{index}.json").write_text(_scan_json(devices), encoding="utf-8")

    stub = bin_dir / "smartctl"
    stub.write_text(
        "#!/bin/bash\n"
        f'n=$(cat "{counter}")\n'
        f'echo $((n + 1)) > "{counter}"\n'
        f"last={len(scans) - 1}\n"
        '[[ "$n" -gt "$last" ]] && n="$last"\n'
        f'cat "{tmp_path}/scan$n.json"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    # /proc/uptime is read by absolute path and cannot be stubbed, so the two
    # branches are selected by moving the grace threshold around the test
    # machine's real uptime: 0 is always "late start", a billion seconds is
    # always "boot-time".
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOMELAB_SMARTCTL_WAIT_STATE": str(state),
        "HOMELAB_SMARTCTL_WAIT_TIMEOUT": str(timeout),
        "HOMELAB_SMARTCTL_WAIT_SETTLE": str(settle),
        "HOMELAB_SMARTCTL_WAIT_POLL": str(poll),
        "HOMELAB_SMARTCTL_WAIT_GRACE": "0" if late_start else "1000000000",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), smartctl or str(stub)],
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        check=False,
    )
    return result, state


def test_stable_list_settles_and_records_it(tmp_path: Path) -> None:
    result, state = run_wait(tmp_path, scans=[["/dev/nvme0", "/dev/sda"]])
    assert result.returncode == 0
    assert "settled" in result.stdout
    assert state.read_text(encoding="utf-8").split() == ["/dev/nvme0", "/dev/sda"]


def test_waits_past_a_long_still_period_for_a_remembered_disk(tmp_path: Path) -> None:
    """The ace regression: the list holds still for far longer than SETTLE first.

    Six identical NVMe-only scans (>= 5x the settle window) precede the SAS
    disks appearing. Without the remembered list the script settles on scan 2
    and the exporter comes up with a poisoned descriptor set.
    """
    result, state = run_wait(
        tmp_path,
        scans=[["/dev/nvme0", "/dev/nvme1"]] * 6
        + [["/dev/nvme0", "/dev/nvme1", "/dev/sda", "/dev/sdb"]],
        remembered=["/dev/nvme0", "/dev/nvme1", "/dev/sda", "/dev/sdb"],
        timeout=60,
    )
    assert result.returncode == 0
    assert "settled" in result.stdout
    assert "/dev/sda" in result.stdout
    assert "/dev/sdb" in result.stdout
    assert state.read_text(encoding="utf-8").split() == [
        "/dev/nvme0",
        "/dev/nvme1",
        "/dev/sda",
        "/dev/sdb",
    ]


def test_extra_disk_beyond_the_remembered_set_is_accepted(tmp_path: Path) -> None:
    """A disk added since the last boot must not make the wait run to timeout."""
    result, state = run_wait(
        tmp_path,
        scans=[["/dev/nvme0", "/dev/sda", "/dev/sdb"]],
        remembered=["/dev/nvme0", "/dev/sda"],
        timeout=30,
    )
    assert result.returncode == 0
    assert "settled" in result.stdout
    assert state.read_text(encoding="utf-8").split() == ["/dev/nvme0", "/dev/sda", "/dev/sdb"]


def test_removed_disk_times_out_once_then_reremembers(tmp_path: Path) -> None:
    """A disk that is genuinely gone costs one boot's timeout, not every boot."""
    result, state = run_wait(
        tmp_path,
        scans=[["/dev/nvme0"]],
        remembered=["/dev/nvme0", "/dev/sda"],
        timeout=2,
    )
    assert result.returncode == 0
    assert "gave up" in result.stdout
    assert state.read_text(encoding="utf-8").split() == ["/dev/nvme0"]

    # Second boot with the corrected expectation settles normally.
    again, _ = run_wait(
        tmp_path,
        scans=[["/dev/nvme0"]],
        remembered=["/dev/nvme0"],
        timeout=30,
    )
    assert again.returncode == 0
    assert "settled" in again.stdout


def test_empty_scan_is_not_treated_as_settled(tmp_path: Path) -> None:
    """No devices is indistinguishable from "not enumerated yet" -- wait it out."""
    result, state = run_wait(tmp_path, scans=[[]], timeout=2)
    assert result.returncode == 0
    assert "gave up" in result.stdout
    assert "none" in result.stdout
    # Nothing useful was learned, so the previous expectation must survive.
    assert not state.exists()


def test_past_grace_skips_the_wait_and_refreshes_state(tmp_path: Path) -> None:
    """Deploy-time restarts must not pay the settle window."""
    result, state = run_wait(
        tmp_path,
        scans=[["/dev/nvme0", "/dev/sda"]],
        remembered=["/dev/nvme0"],
        late_start=True,
        timeout=600,
        settle=600,
    )
    assert result.returncode == 0
    assert "skipping wait" in result.stdout
    assert state.read_text(encoding="utf-8").split() == ["/dev/nvme0", "/dev/sda"]


def test_missing_smartctl_does_not_fail_the_unit(tmp_path: Path) -> None:
    """A broken --smartctl.path must degrade to "start anyway", never exit non-zero."""
    result, _ = run_wait(
        tmp_path,
        scans=[["/dev/sda"]],
        smartctl=str(tmp_path / "no-such-smartctl"),
        timeout=2,
    )
    assert result.returncode == 0
    assert "gave up" in result.stdout


def test_defaults_are_pinned_against_the_unit_timeout() -> None:
    """TimeoutStartSec is sized against these; changing one must force the other."""
    text = SCRIPT.read_text(encoding="utf-8")
    defaults = dict(
        re.findall(
            r'^(\w+)="\$\{HOMELAB_SMARTCTL_WAIT_\w+:-(\d+)\}"',
            text,
            re.MULTILINE,
        )
    )
    assert defaults == {"TIMEOUT": "180", "SETTLE": "20", "POLL": "5", "GRACE": "300"}

    override = (
        ROOT / "metrics-exporters" / "templates" / "smartctl-exporter-override.conf.tpl"
    ).read_text(encoding="utf-8")
    match = re.search(r"^TimeoutStartSec=(\d+)$", override, re.MULTILINE)
    assert match, "drop-in must set TimeoutStartSec"
    assert int(match.group(1)) > int(defaults["TIMEOUT"]), (
        "TimeoutStartSec must exceed the script's own timeout or systemd kills the wait"
    )
    assert "ExecStartPre=/usr/local/bin/homelab-smartctl-wait-devices" in override
