"""Consistency and behaviour guards for the pve-ha-wake-on-change-patch Perl blocks.

The installer patches four pve-ha-manager files by exact block replacement. The
patched LRM/CRM call a method only the patched Env.pm/PVE2.pm add, so the blocks
are checked against each other here, each one is compiled under `use strict`,
and the two wake callbacks are then executed against a fake HA environment —
including the three cases the design depends on: the CRM must not wake on an
LRM status rewrite that left the results alone, the CRM's `crm_commands`
snapshot must be refreshed, and the LRM's `manager_status` snapshot must not be.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "pve-ha-wake-on-change-patch" / "scripts" / "install.sh"
MARKER = "homelab-ha-wake-on-change:"
TARGETS = ("ENV", "PVE2", "LRM", "CRM")


def _text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _blocks() -> dict[str, str]:
    return dict(re.findall(r"^(\w+)=\$\(cat <<'EOF'\n(.*?)\nEOF\n\)$", _text(), re.M | re.S))


def _check_markers() -> dict[str, str]:
    # check_target "${HA_<T>_TARGET}" "${<T>_ORIGINAL}" \\\n  '<marker>' '<label>'
    found = re.findall(
        r'check_target "\$\{HA_(\w+)_TARGET\}" "\$\{\1_ORIGINAL\}" \\\n\s+\'([^\'\n]+)\'',
        _text(),
    )
    return dict(found)


def test_every_target_has_an_original_and_a_patched_block() -> None:
    blocks = _blocks()
    for target in TARGETS:
        assert f"{target}_ORIGINAL" in blocks
        assert f"{target}_PATCHED" in blocks


@pytest.mark.parametrize("target", TARGETS)
def test_marker_appears_once_in_patched_and_never_in_original(target: str) -> None:
    blocks = _blocks()
    marker = _check_markers()[target]

    assert marker.startswith(MARKER)
    assert blocks[f"{target}_PATCHED"].count(MARKER) == 1
    assert blocks[f"{target}_PATCHED"].count(marker) == 1
    assert MARKER not in blocks[f"{target}_ORIGINAL"]


@pytest.mark.parametrize("target", ("LRM", "CRM"))
def test_loop_blocks_keep_their_anchor_lines(target: str) -> None:
    blocks = _blocks()
    original = blocks[f"{target}_ORIGINAL"].splitlines()
    patched = blocks[f"{target}_PATCHED"].splitlines()

    assert patched[0] == original[0]
    assert patched[-1] == original[-1]
    assert "        $haenv->sleep_until($startime + $max_time);" in original
    assert "        $haenv->sleep_until($startime + $max_time, $wake);" in patched


def test_wake_callback_is_forwarded_through_the_env_wrapper() -> None:
    blocks = _blocks()

    assert "return $self->{plug}->sleep_until($end_time, $wake);" in blocks["ENV_PATCHED"]
    assert "my ($self, $end_time, $wake) = @_;" in blocks["PVE2_PATCHED"]


def test_every_called_env_method_is_provided_by_the_patch() -> None:
    blocks = _blocks()
    called = set(re.findall(r"\$haenv->(\w+)\(", blocks["LRM_PATCHED"] + blocks["CRM_PATCHED"]))
    new_methods = {"cfs_file_version"}

    assert new_methods <= called
    for method in new_methods:
        assert f"sub {method} {{" in blocks["ENV_PATCHED"]
        assert f"sub {method} {{" in blocks["PVE2_PATCHED"]


def test_the_superseded_migration_only_patch_is_refused_explicitly() -> None:
    text = _text()

    assert "SUPERSEDED_MARKER='homelab-ha-fast-migrate:'" in text
    assert 'write_status "failed-superseded-${label}"' in text


def test_every_target_is_checked_before_the_first_replacement() -> None:
    text = _text().split("<<'SCRIPT'", 1)[1]
    last_check = max(m.start() for m in re.finditer(r"^\w+_state=\$\(check_target ", text, re.M))
    first_replace = re.search(r"^  replace_block ", text, re.M)

    assert first_replace
    assert last_check < first_replace.start()


def test_reapply_restarts_both_ha_daemons() -> None:
    assert "systemctl try-restart pve-ha-lrm.service pve-ha-crm.service" in _text()


def _compile(source: str) -> subprocess.CompletedProcess[str]:
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl not installed")
    return subprocess.run(
        [perl, "-c", "-"], input=source, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("target", ("ENV", "PVE2"))
def test_patched_subs_compile_under_strict(target: str) -> None:
    source = (
        "package T; use strict; use warnings;\n"
        "sub PVE::Cluster::cfs_update {} sub PVE::Cluster::cfs_file_version {}\n"
        f"{_blocks()[f'{target}_PATCHED']}\n1;\n"
    )
    result = _compile(source)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("target", "locals_"),
    [
        ("LRM", "my ($self, $haenv, $startime, $max_time, $shutdown, $state);"),
        ("CRM", "my ($self, $haenv, $manager, $startime, $max_time, $shutdown, $state);"),
    ],
)
def test_patched_loop_blocks_compile_under_strict(target: str, locals_: str) -> None:
    source = (
        "package T; use strict; use warnings;\n"
        f"sub work {{\n{locals_}\nif (1) {{\n{_blocks()[f'{target}_PATCHED']}\n}}\n}}\n1;\n"
    )
    result = _compile(source)
    assert result.returncode == 0, result.stderr


# A fake $haenv plus `run_block`, which executes a patched loop block inside a
# `work` sub. The fake `sleep_until` calls the wake callback up to six times,
# the way PVE2's once-per-second loop would, and records after how many calls
# it fired (0 = never armed, -1 = armed but never fired). `versions` and `lrm`
# hold one queue per file/node: each lookup shifts the next entry and the last
# one repeats. `version_sub` overrides them to answer per $refresh flag.
_HARNESS = r"""
package FakeEnv;
use strict; use warnings;
sub new { my ($c, %a) = @_; return bless { log => [], versions => {}, lrm => {}, %a }, $c }
sub nodename { $_[0]->{nodename} }
sub log { push $_[0]->{log}->@*, $_[2] }
sub _next {
    my ($queue) = @_;
    return undef if !$queue;
    return @$queue > 1 ? shift @$queue : $queue->[0];
}
sub cfs_file_version {
    my ($s, $filename, $refresh) = @_;
    return $s->{version_sub}->($filename, $refresh) if $s->{version_sub};
    return _next($s->{versions}->{$filename});
}
sub read_lrm_status {
    my ($s, $node) = @_;
    return _next($s->{lrm}->{$node}) // {};
}
sub sleep_until {
    my ($s, $end, $wake) = @_;
    $s->{fired} = 0;
    return if !$wake;
    $s->{fired} = -1;
    for my $i (1 .. 6) { if ($wake->()) { $s->{fired} = $i; return } }
}
package FakeNS;
sub new { my ($c, @n) = @_; return bless { nodes => [@n] }, $c }
sub list_nodes { $_[0]->{nodes} }
package FakeLRM;
sub new { my ($c, %a) = @_; return bless { workers => {}, %a }, $c }
sub update_lrm_status {}
sub check_active_workers {
    my ($s) = @_;
    delete $s->{workers}->{$_} for $s->{finish_after}->();
    return scalar(grep { $_->{pid} } values %{ $s->{workers} });
}
package main;
use strict; use warnings;
"""


def _run(block: str, setup: str) -> str:
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl not installed")
    source = (
        _HARNESS
        + "my ($self, $haenv, $manager);\n"
        + setup
        + "\nsub work {\nmy ($startime, $max_time, $shutdown, $state) = (0, 10, 0, 'active');\n"
        + f"if (1) {{\n{block}\n}}\n}}\n"
        + "work();\nprint $haenv->{fired}, '|', join(';', $haenv->{log}->@*);\n"
    )
    result = subprocess.run(
        [perl, "-"], input=source, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _lrm(setup: str) -> str:
    return _run(_blocks()["LRM_PATCHED"], setup)


def _crm(setup: str) -> str:
    return _run(_blocks()["CRM_PATCHED"], setup)


def test_lrm_wakes_when_the_manager_status_version_changed() -> None:
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'ace',\n"
        "  versions => { 'ha/manager_status' => [5, 5, 6] });\n"
        "$self = FakeLRM->new(finish_after => sub { () });\n"
    )
    assert out == "2|manager status changed, ending LRM round early"


def test_lrm_does_not_snapshot_the_manager_status_version_refreshed() -> None:
    # The round's own data came from the unrefreshed version, so a CRM write
    # that landed while this round worked must still wake it: snapshot 5 (no
    # refresh), first check 6 (refreshed).
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'ace',\n"
        "  version_sub => sub { my ($f, $r) = @_; return $r ? 6 : 5 });\n"
        "$self = FakeLRM->new(finish_after => sub { () });\n"
    )
    assert out == "1|manager status changed, ending LRM round early"


def test_lrm_sleeps_out_the_round_while_nothing_changes() -> None:
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'ace',\n"
        "  versions => { 'ha/manager_status' => [5] });\n"
        "$self = FakeLRM->new(finish_after => sub { () });\n"
    )
    assert out == "-1|"


def test_lrm_wakes_once_one_of_its_workers_exited() -> None:
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'ace',\n"
        "  versions => { 'ha/manager_status' => [5] });\n"
        "my $calls = 0;\n"
        "$self = FakeLRM->new(workers => { 'ct:1' => { pid => 9 } },\n"
        "  finish_after => sub { ++$calls >= 3 ? ('ct:1') : () });\n"
    )
    assert out == "3|a worker finished, ending LRM round early"


def test_lrm_does_not_arm_without_versions_or_workers() -> None:
    # The simulator and test environments have no versioned files, so the
    # regression-test logs must stay byte-identical.
    out = _lrm("$haenv = FakeEnv->new(nodename => 'ace');\n$self = FakeLRM->new();\n")
    assert out == "0|"


def test_lrm_setup_failure_is_logged_and_falls_back_to_the_stock_round() -> None:
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'ace', version_sub => sub { die \"boom\\n\" });\n"
        "$self = FakeLRM->new(finish_after => sub { () });\n"
    )
    assert out == "0|early wake-up setup failed - boom\n"


def test_crm_wakes_when_an_lrm_reported_a_result() -> None:
    out = _crm(
        "$haenv = FakeEnv->new(versions => { 'ha/crm_commands' => [4] },\n"
        "  lrm => { ace => [ { results => {} }, { results => {} }, { results => {} },\n"
        "    { results => { u1 => { exit_code => 0 } } } ] });\n"
        "$manager = { ns => FakeNS->new('ace') };\n"
    )
    assert out == "3|LRM results changed, ending CRM round early"


def test_crm_ignores_an_lrm_status_rewrite_that_kept_the_same_results() -> None:
    # Every LRM rewrites its whole status file each round; waking on that would
    # make the two daemons wake each other once per second forever.
    out = _crm(
        "$haenv = FakeEnv->new(versions => { 'ha/crm_commands' => [4] },\n"
        "  lrm => { ace => [ { timestamp => 1, results => { u1 => { exit_code => 0 } } },\n"
        "    { timestamp => 2, results => { u1 => { exit_code => 0 } } },\n"
        "    { timestamp => 3, results => { u1 => { exit_code => 0 } } } ] });\n"
        "$manager = { ns => FakeNS->new('ace') };\n"
    )
    assert out == "-1|"


def test_crm_wakes_when_a_command_was_queued() -> None:
    out = _crm(
        "$haenv = FakeEnv->new(versions => { 'ha/crm_commands' => [4, 4, 5] },\n"
        "  lrm => { ace => [ {} ] });\n"
        "$manager = { ns => FakeNS->new('ace') };\n"
    )
    assert out == "2|new CRM command queued, ending CRM round early"


def test_crm_snapshots_the_command_version_refreshed() -> None:
    # manage() clears crm_commands itself, so an unrefreshed snapshot (1 here)
    # would differ from every later refreshed read (2) and end every round early.
    out = _crm(
        "$haenv = FakeEnv->new(version_sub => sub { my ($f, $r) = @_; return $r ? 2 : 1 },\n"
        "  lrm => { ace => [ {} ] });\n"
        "$manager = { ns => FakeNS->new('ace') };\n"
    )
    assert out == "-1|"


def test_crm_watches_every_node_the_manager_knows() -> None:
    out = _crm(
        "$haenv = FakeEnv->new(versions => { 'ha/crm_commands' => [4] },\n"
        "  lrm => { ace => [ {} ], bray => [ { results => {} },\n"
        "    { results => { u1 => { exit_code => 0 } } } ] });\n"
        "$manager = { ns => FakeNS->new('ace', 'bray') };\n"
    )
    assert out == "1|LRM results changed, ending CRM round early"


def test_crm_setup_failure_is_logged_and_falls_back_to_the_stock_round() -> None:
    out = _crm(
        "$haenv = FakeEnv->new(version_sub => sub { die \"boom\\n\" });\n"
        "$manager = { ns => FakeNS->new('ace') };\n"
    )
    assert out == "0|early wake-up setup failed - boom\n"
