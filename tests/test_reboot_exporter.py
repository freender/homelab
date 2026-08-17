"""Behavioural tests for metrics-exporters' reboot-textfile-exporter.

The script runs as root on every bare-metal host and its whole job is deciding a
single 0/1, so the cases that matter are the ones where dpkg's output is
misleading: `un` rows with a blank version, `rc` rows carrying a real but stale
version, debug symbol packages, and meta/helper packages whose names encode no
kernel release at all. Each of those was observed on a live host, so they are
reproduced verbatim here rather than paraphrased.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "reboot-textfile-exporter"

# ace/clovis, 2026-08. The `un` row is the unsigned virtual name provided by the
# real -signed package; proxmox-kernel-7.0 is the series meta-package and
# proxmox-kernel-helper is not a kernel at all but sorts highest by version.
PVE_ROWS = """\
un  proxmox-kernel-7.0.14-8-pve
ii  proxmox-kernel-7.0.14-8-pve-signed 7.0.14-8
ii  proxmox-kernel-7.0 7.0.14-8
ii  proxmox-kernel-helper 9.2.0
"""

# cinci, 2026-08: two removed-but-configured kernels alongside the running one.
UBUNTU_ROWS = """\
rc  linux-image-7.0.0-27-generic 7.0.0-27.27
rc  linux-image-unsigned-7.0.0-14-generic 7.0.0-14.14
ii  linux-image-7.0.0-29-generic 7.0.0-29.29
ii  linux-image-generic 7.0.0-29.29
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def run_exporter(
    tmp_path: Path,
    *,
    running_kernel: str,
    linux_image_rows: str = "",
    proxmox_kernel_rows: str = "",
    container: bool = False,
    reboot_flag: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    out_dir = tmp_path / "textfile"
    out_dir.mkdir()

    (tmp_path / "linux-image.rows").write_text(linux_image_rows, encoding="utf-8")
    (tmp_path / "proxmox-kernel.rows").write_text(proxmox_kernel_rows, encoding="utf-8")

    # Only the trailing pattern argument matters; the script queries one pattern
    # per invocation precisely so a no-match exit cannot suppress the other.
    _write_exec(
        stub_dir / "dpkg-query",
        f"""#!/bin/bash
pattern="${{!#}}"
case "$pattern" in
    'linux-image-*') rows="{tmp_path}/linux-image.rows" ;;
    'proxmox-kernel-*') rows="{tmp_path}/proxmox-kernel.rows" ;;
    *) exit 1 ;;
esac
[[ -s "$rows" ]] || exit 1
cat "$rows"
""",
    )
    _write_exec(stub_dir / "uname", f'#!/bin/bash\nprintf "%s\\n" "{running_kernel}"\n')
    _write_exec(stub_dir / "hostname", '#!/bin/bash\nprintf "%s\\n" "testhost"\n')
    _write_exec(
        stub_dir / "systemd-detect-virt",
        f"#!/bin/bash\nexit {0 if container else 1}\n",
    )

    flag_file = tmp_path / "reboot-flag"
    if reboot_flag:
        flag_file.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["TEXTFILE_DIR"] = str(out_dir)
    env["REBOOT_FLAG_FILE"] = str(flag_file)

    result = subprocess.run(
        ["bash", str(EXPORTER)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, out_dir / "reboot.prom"


def metric_value(text: str, name: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(f"{name}{{"):
            return line.rsplit(" ", 1)[1]
    return None


def metric_labels(text: str, name: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(f"{name}{{"):
            return line[line.index("{") + 1 : line.index("}")]
    return None


pytestmark = pytest.mark.skipif(
    shutil.which("dpkg") is None,
    reason="needs dpkg for --compare-versions",
)


def test_pve_up_to_date_reports_zero(tmp_path: Path) -> None:
    """The -signed package is the real one; the `un` and meta rows must not win."""
    result, prom = run_exporter(
        tmp_path, running_kernel="7.0.14-8-pve", proxmox_kernel_rows=PVE_ROWS
    )
    assert result.returncode == 0, result.stderr
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_reboot_required") == "0"
    assert 'installed="7.0.14-8-pve"' in metric_labels(text, "homelab_kernel_info")


def test_pve_pending_kernel_reports_one(tmp_path: Path) -> None:
    rows = PVE_ROWS + "ii  proxmox-kernel-7.0.15-1-pve-signed 7.0.15-1\n"
    _result, prom = run_exporter(
        tmp_path, running_kernel="7.0.14-8-pve", proxmox_kernel_rows=rows
    )
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_reboot_required") == "1"
    labels = metric_labels(text, "homelab_kernel_info")
    assert 'running="7.0.14-8-pve"' in labels
    assert 'installed="7.0.15-1-pve"' in labels


def test_helper_package_never_becomes_the_installed_kernel(tmp_path: Path) -> None:
    """proxmox-kernel-helper 9.2.0 sorts highest by version but is not a kernel."""
    _result, prom = run_exporter(
        tmp_path, running_kernel="7.0.14-8-pve", proxmox_kernel_rows=PVE_ROWS
    )
    labels = metric_labels(prom.read_text(encoding="utf-8"), "homelab_kernel_info")
    assert "9.2.0" not in labels
    assert "helper" not in labels


def test_removed_but_configured_kernel_does_not_fabricate_a_reboot(tmp_path: Path) -> None:
    """An `rc` row newer than the running kernel must be ignored, not counted."""
    rows = UBUNTU_ROWS + "rc  linux-image-7.0.0-31-generic 7.0.0-31.31\n"
    _result, prom = run_exporter(
        tmp_path, running_kernel="7.0.0-29-generic", linux_image_rows=rows
    )
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_reboot_required") == "0"
    assert 'installed="7.0.0-29-generic"' in metric_labels(text, "homelab_kernel_info")


def test_debug_symbol_package_is_not_treated_as_a_kernel(tmp_path: Path) -> None:
    rows = UBUNTU_ROWS + "ii  linux-image-7.0.0-29-generic-dbgsym 7.0.0-29.29\n"
    _result, prom = run_exporter(
        tmp_path, running_kernel="7.0.0-29-generic", linux_image_rows=rows
    )
    labels = metric_labels(prom.read_text(encoding="utf-8"), "homelab_kernel_info")
    assert "dbgsym" not in labels


def test_flag_file_alone_reports_a_pending_reboot(tmp_path: Path) -> None:
    """Where update-notifier-common exists, non-kernel reboots still count."""
    _result, prom = run_exporter(
        tmp_path,
        running_kernel="7.0.0-29-generic",
        linux_image_rows=UBUNTU_ROWS,
        reboot_flag=True,
    )
    assert metric_value(prom.read_text(encoding="utf-8"), "homelab_reboot_required") == "1"


def test_no_installed_kernel_emits_no_reboot_metric(tmp_path: Path) -> None:
    """Better an absent series than a 0 that cannot be told apart from healthy."""
    _result, prom = run_exporter(tmp_path, running_kernel="7.0.14-8-pve")
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_reboot_required") is None
    assert 'installed=""' in metric_labels(text, "homelab_kernel_info")


def test_container_writes_nothing(tmp_path: Path) -> None:
    """LXC guests run the PVE host's kernel; the file map already excludes them."""
    result, prom = run_exporter(
        tmp_path,
        running_kernel="7.0.14-8-pve",
        proxmox_kernel_rows=PVE_ROWS,
        container=True,
    )
    assert result.returncode == 0
    assert not prom.exists()
