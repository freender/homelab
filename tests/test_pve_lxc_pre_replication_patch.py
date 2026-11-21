"""Execution tests for the pve-lxc-pre-replication-patch reapply script.

The script patches pve-container's Migrate.pm and pve-ha-manager's PVECT.pm by
exact block replacement, so any upstream edit to either stanza makes it refuse.
The fixtures are the real upstream subs (trimmed); the generated script runs
against copies of them with its system paths pointed into tmp_path, and the
patched subs are then executed against fakes to check the migration order.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "pve-lxc-pre-replication-patch" / "scripts" / "install.sh"
FIXTURES = ROOT / "tests" / "fixtures" / "pve_lxc_pre_replication"
LXC_MARKER = "run replication before shutting down CT"
HA_MARKER = "let the migration task handle shutdown and start of running CTs"


def _text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _blocks() -> dict[str, str]:
    return dict(re.findall(r"^(\w+)=\$\(cat <<'EOF'\n(.*?)\nEOF\n\)$", _text(), re.M | re.S))


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        pytest.skip(f"{tool} not installed")
    return path


class Host:
    """The generated reapply script, rewired to run unprivileged in tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        _require("perl")
        _require("flock")
        self.migrate = tmp_path / "Migrate.pm"
        self.pvect = tmp_path / "PVECT.pm"
        self.status = tmp_path / "state" / "status"
        shutil.copy(FIXTURES / "Migrate.pm", self.migrate)
        shutil.copy(FIXTURES / "PVECT.pm", self.pvect)
        paths = {
            "LXC_MIGRATE_TARGET": self.migrate,
            "HA_PVECT_TARGET": self.pvect,
            "STATE_DIR": tmp_path / "state",
            "BACKUP_DIR": tmp_path / "backup",
            "TARGET_CHECKSUM_DIR": tmp_path / "checksums",
            "LOCK_FILE": tmp_path / "patches.lock",
        }
        script = _text().split("<<'SCRIPT'\n", 1)[1].split("\nSCRIPT\n", 1)[0]
        for name, path in paths.items():
            script, count = re.subn(rf"^{name}=/\S+$", f"{name}={path}", script, flags=re.M)
            assert count == 1, name
        self.script = tmp_path / "patch.sh"
        self.script.write_text(script + "\n", encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.script), *args], capture_output=True, text=True, check=False
        )

    def state(self) -> str:
        match = re.search(r"^state=(\S+)$", self.status.read_text(encoding="utf-8"), re.M)
        assert match
        return match.group(1)


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def test_original_blocks_match_the_upstream_fixtures_exactly_once() -> None:
    blocks = _blocks()

    assert (FIXTURES / "Migrate.pm").read_text().count(blocks["LXC_ORIGINAL"]) == 1
    assert (FIXTURES / "PVECT.pm").read_text().count(blocks["HA_ORIGINAL"]) == 1


def test_patched_lxc_block_keeps_the_upstream_shutdown_stanza_verbatim() -> None:
    blocks = _blocks()

    assert blocks["LXC_PATCHED"].endswith("\n\n" + blocks["LXC_ORIGINAL"])
    assert blocks["LXC_PATCHED"].count(LXC_MARKER) == 1
    assert LXC_MARKER not in blocks["LXC_ORIGINAL"]


def test_stock_targets_are_patched_and_still_compile(host: Host) -> None:
    blocks = _blocks()
    result = host.run()

    assert result.returncode == 0, result.stderr
    assert host.state() == "patched"
    assert host.migrate.read_text() == (FIXTURES / "Migrate.pm").read_text().replace(
        blocks["LXC_ORIGINAL"], blocks["LXC_PATCHED"]
    )
    assert host.pvect.read_text() == (FIXTURES / "PVECT.pm").read_text().replace(
        blocks["HA_ORIGINAL"], blocks["HA_PATCHED"]
    )
    for target in (host.migrate, host.pvect):
        assert subprocess.run(["perl", "-c", str(target)], capture_output=True).returncode == 0


def test_second_run_is_already_patched_and_leaves_targets_alone(host: Host) -> None:
    assert host.run().returncode == 0
    before = (host.migrate.read_text(), host.pvect.read_text())

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert host.state() == "already-patched"
    assert (host.migrate.read_text(), host.pvect.read_text()) == before


def test_hook_run_skips_when_neither_target_changed(host: Host) -> None:
    assert host.run().returncode == 0

    result = host.run("--if-target-changed")

    assert result.returncode == 0
    assert "targets unchanged; skipping" in result.stdout


@pytest.mark.parametrize(
    ("target", "stock", "drifted", "label"),
    [
        # pve-container <= 6.1.23 had no eval around vm_stop.
        (
            "migrate",
            "        eval { PVE::LXC::vm_stop($vmid, 0, $timeout) };",
            "        PVE::LXC::vm_stop($vmid, 0, $timeout);",
            "lxc_migrate",
        ),
        # pve-ha-manager <= 5.2.5 had no migrate_options passthrough.
        (
            "pvect",
            "    $migrate_options //= {};\n",
            "",
            "ha_pvect",
        ),
    ],
)
def test_upstream_drift_refuses_without_touching_the_target(
    host: Host, target: str, stock: str, drifted: str, label: str
) -> None:
    path: Path = getattr(host, target)
    text = path.read_text()
    assert stock in text
    path.write_text(text.replace(stock, drifted))
    before = path.read_text()

    result = host.run()

    assert result.returncode != 0
    assert f"unexpected {label} migration stanza" in result.stderr
    assert host.state() == f"failed-unexpected-{label}"
    assert path.read_text() == before


# Fakes for every package the patched subs call; each call is appended to @::calls.
_FAKES = r"""
use strict; use warnings;
our @calls;
package PVE::LXC;
sub check_running { push @::calls, 'check_running'; return $::running }
sub vm_stop { push @::calls, "vm_stop:$_[2]"; die "stop failed\n" if $::stop_fails }
package PVE::Tunnel;
sub finish_tunnel { push @::calls, 'finish_tunnel' }
package PVE::ReplicationConfig;
sub new { return bless {}, shift }
sub find_local_replication_job { push @::calls, "find_job:$_[2]"; return $::job }
package PVE::Replication;
sub run_replication { push @::calls, "run_replication:$_[0]" }
package FakeMigrate;
sub new {
    my ($c, %o) = @_;
    return bless { node => 'clovis', opts => \%o, ($o{remote} ? (tunnel => 'T') : ()) }, $c;
}
sub log { my $msg = $_[2]; chomp $msg; push @::calls, "log:$_[1]:$msg" }
sub prepare_local_cluster_checks { push @::calls, 'local_checks' }
package main;
"""


def _run_prepare(host: Host, setup: str, opts: str) -> list[str]:
    assert host.run().returncode == 0
    source = (
        _FAKES
        + f"our ($running, $job, $stop_fails) = (1, {{ id => '101-0' }}, 0);\n{setup}\n"
        + f"do '{host.migrate}' or die $@ || $!;\n"
        + f"my $self = FakeMigrate->new({opts});\n"
        + 'eval { PVE::LXC::Migrate::prepare($self, 101) }; push @calls, "died:$@" if $@;\n'
        + 'print join("\\n", @calls);\n'
    )
    result = subprocess.run(["perl", "-"], input=source, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_restart_migration_replicates_before_shutdown(host: Host) -> None:
    calls = _run_prepare(host, "", "restart => 1")

    assert calls == [
        "check_running",
        "local_checks",
        "find_job:clovis",
        f"log:info:{LXC_MARKER} 101",
        "run_replication:PVE::LXC::Config",
        "log:info:shutdown CT 101",
        "vm_stop:180",
    ]


@pytest.mark.parametrize(
    ("setup", "opts"),
    [
        ("$job = undef;", "restart => 1"),
        ("$running = 0;", "restart => 1"),
        ("", "restart => 1, remote => { conn => 1 }"),
    ],
    ids=["no-replication-job", "stopped-ct", "remote-migration"],
)
def test_pre_replication_is_skipped_outside_its_scope(host: Host, setup: str, opts: str) -> None:
    calls = _run_prepare(host, setup, opts)

    assert not any(call.startswith("run_replication") for call in calls)


def test_failed_shutdown_still_cleans_up_a_remote_tunnel(host: Host) -> None:
    calls = _run_prepare(host, "$stop_fails = 1;", "restart => 1, remote => { conn => 1 }")

    assert calls[-3:] == ["vm_stop:180", "finish_tunnel", "died:stop failed"]


def test_ha_migrate_hands_shutdown_to_the_migration_task(host: Host) -> None:
    assert host.run().returncode == 0
    source = (
        "use strict; use warnings; our @calls;\n"
        "package PVE::API2::LXC;\n"
        "sub migrate_vm { my ($c, $p) = @_;\n"
        "  push @::calls, join ',', map { \"$_=$p->{$_}\" } sort keys %$p; return 'UPID' }\n"
        "package PVE::HA::Tools; sub upid_wait {}\n"
        "package FakeEnv; sub nodename { 'ace' }\n"
        "package main;\n"
        f"do '{host.pvect}' or die $@ || $!;\n"
        "no strict 'refs';\n"
        "*{'PVE::HA::Resources::PVECT::check_running'} = sub { push @calls, 'check_running'; 1 };\n"
        "*{'PVE::HA::Resources::PVECT::shutdown'} = sub { push @calls, 'shutdown' };\n"
        "*{'PVE::HA::Resources::PVECT::config_file'} = sub { '/nonexistent' };\n"
        "PVE::HA::Resources::PVECT->migrate(bless({}, 'FakeEnv'), 101, 'clovis', 0,\n"
        "  { 'skip-config-check' => 1 });\n"
        'print join("\\n", @calls);\n'
    )
    result = subprocess.run(["perl", "-"], input=source, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "node=ace,online=0,restart=1,skip-config-check=1,target=clovis,vmid=101"
    ]
