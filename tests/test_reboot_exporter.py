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

from homelab.modules.metrics_exporters import PVE_PATCH_STATUSES, pve_patch_statuses

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

# riven, 2026-10: Debian 13 stock kernels name the release `+debN`, not `-N`,
# beside the linux-image-amd64 meta-package (freender/homelab-ops#134).
DEBIAN13_ROWS = """\
ii  linux-image-6.12.111+deb13-amd64 6.12.111-1
ii  linux-image-amd64 6.12.111-1
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
    patch_manifest: str | None = None,
    patch_statuses: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    out_dir = tmp_path / "textfile"
    out_dir.mkdir(exist_ok=True)

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

    manifest_file = tmp_path / "pve-patch-statuses.conf"
    if patch_manifest is not None:
        manifest_file.write_text(patch_manifest, encoding="utf-8")
    for path, content in (patch_statuses or {}).items():
        status_file = tmp_path / path
        status_file.write_text(content, encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["TEXTFILE_DIR"] = str(out_dir)
    env["REBOOT_FLAG_FILE"] = str(flag_file)
    env["PVE_PATCH_MANIFEST"] = str(manifest_file)

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


@pytest.mark.parametrize(
    ("extra_rows", "installed", "pending"),
    [
        ("", "6.12.111+deb13-amd64", "0"),
        ("ii  linux-image-6.12.111+deb13-amd64-unsigned 6.12.111-1\n", "6.12.111+deb13-amd64", "0"),
        ("ii  linux-image-6.12.112+deb13-amd64 6.12.112-1\n", "6.12.112+deb13-amd64", "1"),
    ],
    ids=["up-to-date", "unsigned-twin", "pending"],
)
def test_debian13_kernel_release_is_recognised(
    tmp_path: Path, extra_rows: str, installed: str, pending: str
) -> None:
    _result, prom = run_exporter(
        tmp_path,
        running_kernel="6.12.111+deb13-amd64",
        linux_image_rows=DEBIAN13_ROWS + extra_rows,
    )
    text = prom.read_text(encoding="utf-8")
    assert metric_value(text, "homelab_reboot_required") == pending
    assert f'installed="{installed}"' in metric_labels(text, "homelab_kernel_info")


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


def test_pve_patch_health_reports_only_allowed_statuses(tmp_path: Path) -> None:
    manifest = f"""\
zfs-migration-sync|{tmp_path}/migration|patched,already-patched,repaired-partial-patch
zfs-large-block|{tmp_path}/large-block|patched,already-patched
lxc-pre-replication|{tmp_path}/pre-replication|patched,already-patched
"""
    result, _prom = run_exporter(
        tmp_path,
        running_kernel="7.0.14-8-pve",
        proxmox_kernel_rows=PVE_ROWS,
        patch_manifest=manifest,
        patch_statuses={
            "migration": "state=repaired-partial-patch\n",
            "large-block": "state=failed-validation\n",
            "pre-replication": "state=patched\nstate=already-patched\n",
        },
    )

    assert result.returncode == 0, result.stderr
    text = (tmp_path / "textfile" / "pve-patches.prom").read_text(encoding="utf-8")
    assert 'homelab_pve_patch_healthy{patch="zfs-migration-sync"} 1' in text
    assert 'homelab_pve_patch_healthy{patch="zfs-large-block"} 0' in text
    assert 'homelab_pve_patch_healthy{patch="lxc-pre-replication"} 0' in text


def test_absent_pve_patch_manifest_removes_stale_metric(tmp_path: Path) -> None:
    out_dir = tmp_path / "textfile"
    out_dir.mkdir()
    stale_metric = out_dir / "pve-patches.prom"
    stale_metric.write_text("stale\n", encoding="utf-8")

    result, _prom = run_exporter(
        tmp_path,
        running_kernel="7.0.14-8-pve",
        proxmox_kernel_rows=PVE_ROWS,
    )

    assert result.returncode == 0, result.stderr
    assert not stale_metric.exists()


def _marker_case(tmp_path: Path, markers: str, target_text: str) -> str:
    target = tmp_path / "Target.pm"
    target.write_text(target_text, encoding="utf-8")
    manifest = f"p|{tmp_path}/status|patched{markers.replace('TARGET', str(target))}\n"
    result, _prom = run_exporter(
        tmp_path,
        running_kernel="7.0.14-8-pve",
        proxmox_kernel_rows=PVE_ROWS,
        patch_manifest=manifest,
        patch_statuses={"status": "state=patched\n"},
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "textfile" / "pve-patches.prom").read_text(encoding="utf-8")
    return metric_value(text, "homelab_pve_patch_healthy") or ""


PATCHED_PERL = "        $unmount_received_subvol->();\n    my $cmd = ['zfs', 'send', '-RpvUL'];\n"


@pytest.mark.parametrize(
    ("markers", "target_text", "expected"),
    [
        # Status-only manifest line (pre-marker format) keeps the old behaviour.
        ("", "", "1"),
        ("|TARGET|$unmount_received_subvol->();", PATCHED_PERL, "1"),
        (
            "|TARGET|$unmount_received_subvol->();|TARGET|['zfs', 'send', '-RpvUL']",
            PATCHED_PERL,
            "1",
        ),
        # apt replaced the target and the reapply was skipped: the status file
        # still says patched, but the live file has lost the marker.
        ("|TARGET|$unmount_received_subvol->();", "    eval { zfs recv };\n", "0"),
        # One of several targets unpatched fails the whole patch.
        (
            "|TARGET|$unmount_received_subvol->();|TARGET|['zfs', 'send', '-RpvUL']",
            "        $unmount_received_subvol->();\n",
            "0",
        ),
        ("|/nonexistent/Target.pm|$unmount_received_subvol->();", PATCHED_PERL, "0"),
        ("|TARGET", PATCHED_PERL, "0"),
        ("|relative/Target.pm|marker", PATCHED_PERL, "0"),
        ("|TARGET|", PATCHED_PERL, "0"),
    ],
)
def test_pve_patch_health_requires_marker_in_live_target(
    tmp_path: Path, markers: str, target_text: str, expected: str
) -> None:
    assert _marker_case(tmp_path, markers, target_text) == expected


def test_pve_patch_manifest_is_derived_from_enabled_features() -> None:
    assert [entry[0] for entry in pve_patch_statuses(ROOT, "ace")] == [
        "zfs-migration-sync",
        "zfs-large-block",
        "lxc-pre-replication",
    ]
    assert [entry[0] for entry in pve_patch_statuses(ROOT, "bray")] == [
        "zfs-migration-sync",
        "lxc-pre-replication",
    ]
    assert pve_patch_statuses(ROOT, "osiris") == []


def test_pve_patch_manifest_renders_target_marker_pairs() -> None:
    by_patch = {entry[0]: entry[3] for entry in pve_patch_statuses(ROOT, "ace")}
    assert by_patch["lxc-pre-replication"] == (
        "/usr/share/perl5/PVE/LXC/Migrate.pm|pre-stop replication before shutdown|"
        "/usr/share/perl5/PVE/HA/Resources/PVECT.pm|can run pre-stop replication before shutdown"
    )


@pytest.mark.parametrize("entry", PVE_PATCH_STATUSES, ids=lambda entry: entry[1])
def test_pve_patch_markers_match_installer(entry: tuple) -> None:
    """A marker the installer never writes would report every host unhealthy."""
    feature, _label, _status, _states, markers = entry
    installer = (ROOT / feature / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert markers
    for target, marker in markers:
        assert "|" not in target + marker and "\n" not in target + marker
        assert target in installer
        assert marker in installer
