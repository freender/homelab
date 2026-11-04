"""Consistency guards for the pve-ha-fast-migrate-patch Perl blocks.

The installer patches four pve-ha-manager files by exact block replacement. The
patched LRM/CRM call methods only the patched Env.pm/PVE2.pm add, so the blocks
are checked against each other here, and each one is compiled under
`use strict` so an undeclared variable or a syntax slip fails before a deploy.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "pve-ha-fast-migrate-patch" / "scripts" / "install.sh"
MARKER = "homelab-ha-fast-migrate:"
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
    new_methods = {"manager_status_version"}

    assert new_methods <= called
    for method in new_methods:
        assert f"sub {method} {{" in blocks["ENV_PATCHED"]
        assert f"sub {method} {{" in blocks["PVE2_PATCHED"]


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
# it fired (0 = never armed, -1 = armed but never fired).
_HARNESS = r"""
package FakeEnv;
use strict; use warnings;
sub new { my ($c, %a) = @_; return bless { log => [], %a }, $c }
sub nodename { $_[0]->{nodename} }
sub log { push $_[0]->{log}->@*, $_[2] }
sub manager_status_version {
    my ($s) = @_; my $v = $s->{versions}; return @$v > 1 ? shift @$v : $v->[0];
}
sub read_manager_status { $_[0]->{ms}->[ $_[0]->{ms_reads}++ ] }
sub read_lrm_status { my ($s) = @_; my $r = $s->{lrm}; return @$r > 1 ? shift @$r : $r->[0] }
sub sleep_until {
    my ($s, $end, $wake) = @_;
    $s->{fired} = 0;
    return if !$wake;
    $s->{fired} = -1;
    for my $i (1 .. 6) { if ($wake->()) { $s->{fired} = $i; return } }
}
package FakeLRM;
sub new { my ($c, %a) = @_; return bless { %a }, $c }
sub update_lrm_status {}
sub check_active_workers { my ($s) = @_; delete $s->{workers}->{$_} for $s->{finish_after}->(); }
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


def test_lrm_without_migration_sleeps_out_the_stock_round() -> None:
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'ace', versions => [5]);\n"
        "$self = FakeLRM->new(workers => { 'ct:1' => { pid => 9, state => 'started' } },\n"
        "  service_status => { 'ct:1' => { state => 'started', node => 'ace', uid => 'a' } },\n"
        "  finish_after => sub { () });\n"
    )
    assert out == "0|"


def test_lrm_source_wakes_once_its_migrate_worker_exited() -> None:
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'ace', versions => [5]);\n"
        "my $calls = 0;\n"
        "$self = FakeLRM->new(workers => { 'ct:1' => { pid => 9, state => 'migrate' } },\n"
        "  service_status => {}, finish_after => sub { ++$calls >= 3 ? ('ct:1') : () });\n"
    )
    assert out == "3|migration of 'ct:1' finished, ending LRM round early"


def test_lrm_target_wakes_only_when_the_incoming_service_changed() -> None:
    # Version bumps at call 2 without touching ct:1 (another CRM write), then
    # at call 4 with ct:1 handed over under a new uid.
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'clovis', versions => [5, 5, 6, 6, 7],\n"
        "  ms => [ { service_status => { 'ct:1' => { uid => 'a' } } },\n"
        "          { service_status => { 'ct:1' => { uid => 'b' } } } ]);\n"
        "$self = FakeLRM->new(workers => {}, finish_after => sub { () },\n"
        "  service_status => { 'ct:1' => { state => 'migrate', node => 'ace',\n"
        "    target => 'clovis', uid => 'a' } });\n"
    )
    assert out == "4|service 'ct:1' handed over, ending LRM round early"


def test_lrm_on_an_uninvolved_node_does_not_arm() -> None:
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'bray', versions => [5]);\n"
        "$self = FakeLRM->new(workers => {}, finish_after => sub { () },\n"
        "  service_status => { 'ct:1' => { state => 'migrate', node => 'ace',\n"
        "    target => 'clovis', uid => 'a' } });\n"
    )
    assert out == "0|"


def test_lrm_setup_failure_is_logged_and_falls_back_to_the_stock_round() -> None:
    out = _lrm(
        "$haenv = FakeEnv->new(nodename => 'clovis', versions => [5]);\n"
        "no warnings 'redefine';\n"
        "*FakeEnv::manager_status_version = sub { die \"boom\\n\" };\n"
        "$self = FakeLRM->new(workers => {}, finish_after => sub { () },\n"
        "  service_status => { 'ct:1' => { state => 'migrate', target => 'clovis',\n"
        "    uid => 'a' } });\n"
    )
    assert out == "0|migration wake-up setup failed - boom\n"


def test_crm_wakes_when_the_source_lrm_reports_the_result() -> None:
    out = _crm(
        "$haenv = FakeEnv->new(lrm => [ { results => {} }, { results => {} },\n"
        "  { results => {} }, { results => { u1 => { exit_code => 0 } } } ]);\n"
        "$manager = { ss => { 'ct:1' => { state => 'migrate', node => 'ace', uid => 'u1' },\n"
        "  'ct:2' => { state => 'started', node => 'ace', uid => 'u2' } } };\n"
    )
    assert out == "3|LRM reported result for 'ct:1', ending CRM round early"


def test_crm_does_not_arm_for_a_result_that_is_already_there() -> None:
    # Guards against spinning on a result the manager ignores (offline node).
    out = _crm(
        "$haenv = FakeEnv->new(lrm => [ { results => { u1 => { exit_code => 0 } } } ]);\n"
        "$manager = { ss => { 'ct:1' => { state => 'migrate', node => 'ace', uid => 'u1' } } };\n"
    )
    assert out == "0|"


def test_crm_without_migration_sleeps_out_the_stock_round() -> None:
    out = _crm(
        "$haenv = FakeEnv->new(lrm => [ {} ]);\n"
        "$manager = { ss => { 'ct:1' => { state => 'started', node => 'ace', uid => 'u1' } } };\n"
    )
    assert out == "0|"
