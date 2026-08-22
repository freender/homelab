"""Behavioral tests for the file-installation half of `lib/utils.sh`.

`test_safety_regressions.py` covers the systemd helpers (pause, retire, failed-unit
recovery, masking). This file covers the other, larger half: the file-map reader and
the copy/install/backup primitives that nearly every `scripts/install.sh` calls, as
root, on every host.

Everything here runs real bash against `lib/utils.sh` in a tmp_path sandbox. The only
stub is `systemctl`, and only for `ensure_timer_state` — the file helpers touch
nothing but the filesystem, so they are exercised directly.

Why these matter enough to test at this level:
  * `prune_backup_history` deletes files. A glob or sort regression deletes the wrong
    ones, or the live config itself.
  * `install_build_file_validated` is the rollback path for configs that can only be
    checked after installation (sshd drop-ins). If rollback breaks, a bad config
    survives the deploy and locks us out on the next service reload.
  * `load_file_map` is the bash side of a cross-language contract with
    `module_support.write_file_map`. Nothing else pins the two together.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from homelab.module_support import FileSpec, write_file_map

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / "lib" / "utils.sh"

SYSTEMCTL_STUB = """
systemctl() {
    printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
    case "$1" in
        is-enabled) [[ "${SYSTEMCTL_IS_ENABLED:-enabled}" == "enabled" ]] || return 1 ;;
        is-active) [[ "${SYSTEMCTL_IS_ACTIVE:-inactive}" == "active" ]] || return 1 ;;
    esac
    return 0
}
"""


def run_utils(
    snippet: str,
    *,
    env: dict[str, str] | None = None,
    stub_systemctl: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet with lib/utils.sh sourced.

    No `set -e`: the helpers use the 0=changed / 1=unchanged / 2=error convention,
    so a non-zero status is a normal result, not a failure. Snippets echo `rc=$?`
    and tests assert on it.
    """
    script = f"source {shlex.quote(str(UTILS))}\n"
    if stub_systemctl:
        script += SYSTEMCTL_STUB
    script += snippet + "\n"
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def rc_of(result: subprocess.CompletedProcess[str]) -> int:
    """Extract the `rc=N` marker a snippet echoed."""
    for line in result.stdout.splitlines():
        if line.startswith("rc="):
            return int(line.removeprefix("rc="))
    raise AssertionError(f"no rc= marker in output:\n{result.stdout}\n{result.stderr}")


# --------------------------------------------------------------------------
# file_needs_update — the predicate every copy/install helper delegates to
# --------------------------------------------------------------------------


def test_file_needs_update_reports_missing_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("a\n", encoding="utf-8")

    result = run_utils(f'file_needs_update "{src}" "{tmp_path}/absent"; echo "rc=$?"')

    assert rc_of(result) == 0


def test_file_needs_update_reports_identical_content_as_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("a\n", encoding="utf-8")
    dst.write_text("a\n", encoding="utf-8")

    result = run_utils(f'file_needs_update "{src}" "{dst}"; echo "rc=$?"')

    assert rc_of(result) == 1


def test_file_needs_update_detects_differing_content(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("a\n", encoding="utf-8")
    dst.write_text("b\n", encoding="utf-8")

    result = run_utils(f'file_needs_update "{src}" "{dst}"; echo "rc=$?"')

    assert rc_of(result) == 0


def test_force_update_overrides_identical_content(tmp_path: Path) -> None:
    """FORCE_UPDATE is what `./deploy --force` relies on to re-push byte-identical
    files; if it stopped short-circuiting the compare, --force would be a no-op."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("a\n", encoding="utf-8")
    dst.write_text("a\n", encoding="utf-8")

    result = run_utils(
        f'file_needs_update "{src}" "{dst}"; echo "rc=$?"',
        env={"FORCE_UPDATE": "true"},
    )

    assert rc_of(result) == 0


def test_file_needs_update_errors_on_missing_source(tmp_path: Path) -> None:
    """rc=2 must be distinguishable from rc=1: a missing build artifact is a bug,
    not a no-op, and callers propagate 2 while swallowing 1."""
    result = run_utils(
        f'file_needs_update "{tmp_path}/absent" "{tmp_path}/dst"; echo "rc=$?"'
    )

    assert rc_of(result) == 2
    assert "source file not found" in result.stderr


# --------------------------------------------------------------------------
# copy_if_changed / install_if_changed
# --------------------------------------------------------------------------


def test_copy_if_changed_writes_and_reports_changed(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("new\n", encoding="utf-8")
    dst.write_text("old\n", encoding="utf-8")

    result = run_utils(f'copy_if_changed "{src}" "{dst}"; echo "rc=$?"')

    assert rc_of(result) == 0
    assert dst.read_text(encoding="utf-8") == "new\n"


def test_copy_if_changed_leaves_destination_alone_when_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("same\n", encoding="utf-8")
    dst.write_text("same\n", encoding="utf-8")
    before = dst.stat().st_mtime_ns

    result = run_utils(f'copy_if_changed "{src}" "{dst}"; echo "rc=$?"')

    assert rc_of(result) == 1
    assert dst.stat().st_mtime_ns == before


def test_copy_if_changed_propagates_error_without_touching_destination(
    tmp_path: Path,
) -> None:
    dst = tmp_path / "dst"
    dst.write_text("keep\n", encoding="utf-8")

    result = run_utils(f'copy_if_changed "{tmp_path}/absent" "{dst}"; echo "rc=$?"')

    assert rc_of(result) == 2
    assert dst.read_text(encoding="utf-8") == "keep\n"


def test_install_if_changed_creates_parent_directory_and_sets_mode(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "nested" / "deep" / "dst"
    src.write_text("x\n", encoding="utf-8")

    result = run_utils(f'install_if_changed "{src}" "{dst}" 600; echo "rc=$?"')

    assert rc_of(result) == 0
    assert dst.read_text(encoding="utf-8") == "x\n"
    assert oct(dst.stat().st_mode & 0o777) == "0o600"


def test_install_if_changed_repairs_mode_drift_on_unchanged_content(
    tmp_path: Path,
) -> None:
    """Unchanged content still re-applies the mode.

    This is deliberate and load-bearing: a secret env file whose permissions were
    widened out-of-band would otherwise stay world-readable forever, because its
    content never changes and the content-compare short-circuits first.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("secret\n", encoding="utf-8")
    dst.write_text("secret\n", encoding="utf-8")
    dst.chmod(0o644)

    result = run_utils(f'install_if_changed "{src}" "{dst}" 600; echo "rc=$?"')

    assert rc_of(result) == 1
    assert oct(dst.stat().st_mode & 0o777) == "0o600"


def make_unwritable_dest(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "src"
    src.write_text("x\n", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    return src, locked / "dst"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize(
    "call",
    [
        'copy_if_changed "$SRC" "$DST"',
        'install_if_changed "$SRC" "$DST" 644',
        'backup_and_copy_if_changed "$SRC" "$DST"',
        'backup_and_install_if_changed "$SRC" "$DST" 644',
    ],
)
def test_write_failures_are_reported_as_errors_not_success(
    tmp_path: Path, call: str
) -> None:
    """A failed write must return 2, never 0.

    Regression: these helpers used to run `cp`/`install` unchecked and then
    unconditionally `return 0` with an "Updated" message. A write that failed
    (read-only mount, ENOSPC, immutable attr, wrong ownership) was therefore
    reported as a successful *change*, which is worse than a plain false success:
    installers feed that status into `homelab_reload_and_clear_failed` as their
    `changed` flag, so a deploy that wrote nothing would `reset-failed` the very
    units it failed to update — clearing failed-unit alerting on the strength of
    a fix that never landed.
    """
    src, dst = make_unwritable_dest(tmp_path)

    result = run_utils(
        f'{call}; echo "rc=$?"', env={"SRC": str(src), "DST": str(dst)}
    )

    assert rc_of(result) == 2
    assert not dst.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_install_file_map_propagates_a_write_failure(tmp_path: Path) -> None:
    """install_file_map must surface rc=2 rather than folding it into 'changed'."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "a.conf").write_text("a\n", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    (build_dir / "file-map.conf").write_text(
        f"a.conf|{locked / 'a.conf'}|644\n", encoding="utf-8"
    )

    result = run_utils(
        f'load_file_map "{build_dir}/file-map.conf"\n'
        f'install_file_map "{build_dir}"; echo "rc=$?"'
    )

    assert rc_of(result) == 2


# --------------------------------------------------------------------------
# backup_config / prune_backup_history — the helpers that delete things
# --------------------------------------------------------------------------


def test_backup_config_is_a_noop_for_missing_path(tmp_path: Path) -> None:
    result = run_utils(f'backup_config "{tmp_path}/absent"; echo "rc=$?"')

    assert rc_of(result) == 0
    assert list(tmp_path.iterdir()) == []


def test_backup_config_copies_directories_recursively(tmp_path: Path) -> None:
    target = tmp_path / "conf.d"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "a.conf").write_text("a\n", encoding="utf-8")

    result = run_utils(f'backup_config "{target}"; echo "rc=$?"')

    assert rc_of(result) == 0
    backups = list(tmp_path.glob("conf.d.bak.*"))
    assert len(backups) == 1
    assert (backups[0] / "sub" / "a.conf").read_text(encoding="utf-8") == "a\n"


def test_backup_and_copy_if_changed_snapshots_the_previous_contents(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("new\n", encoding="utf-8")
    dst.write_text("old\n", encoding="utf-8")

    result = run_utils(f'backup_and_copy_if_changed "{src}" "{dst}"; echo "rc=$?"')

    assert rc_of(result) == 0
    assert dst.read_text(encoding="utf-8") == "new\n"
    backups = list(tmp_path.glob("dst.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old\n"


def test_backup_and_install_if_changed_snapshots_and_sets_mode(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("new\n", encoding="utf-8")
    dst.write_text("old\n", encoding="utf-8")

    result = run_utils(
        f'backup_and_install_if_changed "{src}" "{dst}" 640; echo "rc=$?"'
    )

    assert rc_of(result) == 0
    assert oct(dst.stat().st_mode & 0o777) == "0o640"
    assert [p.read_text(encoding="utf-8") for p in tmp_path.glob("dst.bak.*")] == ["old\n"]


def _seed_backups(target: Path, stamps: list[str]) -> None:
    target.write_text("live\n", encoding="utf-8")
    for stamp in stamps:
        target.with_name(f"{target.name}.bak.{stamp}").write_text(
            stamp, encoding="utf-8"
        )


def test_prune_backup_history_keeps_the_newest_and_deletes_the_rest(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.conf"
    stamps = [f"2024010100000{n}" for n in range(1, 6)]
    _seed_backups(target, stamps)

    result = run_utils(f'prune_backup_history "{target}" 2; echo "rc=$?"')

    assert rc_of(result) == 0
    survivors = sorted(p.name for p in tmp_path.glob("app.conf.bak.*"))
    assert survivors == ["app.conf.bak.20240101000004", "app.conf.bak.20240101000005"]


def test_prune_backup_history_never_touches_the_live_file(tmp_path: Path) -> None:
    """The glob is `${path}.bak.*`. A regression that widened it to `${path}*`
    would delete the config being backed up."""
    target = tmp_path / "app.conf"
    _seed_backups(target, [f"2024010100000{n}" for n in range(1, 6)])

    run_utils(f'prune_backup_history "{target}" 1')

    assert target.read_text(encoding="utf-8") == "live\n"


def test_prune_backup_history_ignores_unrelated_neighbours(tmp_path: Path) -> None:
    target = tmp_path / "app.conf"
    _seed_backups(target, [f"2024010100000{n}" for n in range(1, 6)])
    bystander = tmp_path / "other.conf.bak.20240101000001"
    bystander.write_text("other\n", encoding="utf-8")

    run_utils(f'prune_backup_history "{target}" 1')

    assert bystander.exists()


def test_prune_backup_history_is_a_noop_below_the_keep_count(tmp_path: Path) -> None:
    target = tmp_path / "app.conf"
    _seed_backups(target, ["20240101000001", "20240101000002"])

    run_utils(f'prune_backup_history "{target}" 3')

    assert len(list(tmp_path.glob("app.conf.bak.*"))) == 2


def test_prune_backup_history_falls_back_to_three_on_garbage_keep_count(
    tmp_path: Path,
) -> None:
    """A non-numeric count must not be treated as zero.

    `(( ${#backups[@]} <= keep_count ))` with an empty/garbage count would compare
    against 0 and wipe every backup — exactly when you most want them.
    """
    target = tmp_path / "app.conf"
    _seed_backups(target, [f"2024010100000{n}" for n in range(1, 6)])

    run_utils(f'prune_backup_history "{target}" "not-a-number"')

    assert len(list(tmp_path.glob("app.conf.bak.*"))) == 3


def test_backup_config_prunes_via_backup_keep_count(tmp_path: Path) -> None:
    target = tmp_path / "app.conf"
    _seed_backups(target, [f"2024010100000{n}" for n in range(1, 6)])

    run_utils(f'backup_config "{target}"', env={"BACKUP_KEEP_COUNT": "2"})

    assert len(list(tmp_path.glob("app.conf.bak.*"))) == 2


# --------------------------------------------------------------------------
# file-map: the bash reader and its contract with Python's writer
# --------------------------------------------------------------------------


def write_map(tmp_path: Path, body: str) -> Path:
    map_file = tmp_path / "file-map.conf"
    map_file.write_text(body, encoding="utf-8")
    return map_file


def test_load_file_map_parses_entries_and_defaults_the_mode(tmp_path: Path) -> None:
    map_file = write_map(
        tmp_path,
        "unit.service|/etc/systemd/system/unit.service|644\nscript.sh|/usr/local/bin/script.sh\n",
    )

    result = run_utils(
        f'load_file_map "{map_file}"\n'
        'mapped_dest unit.service\n'
        'mapped_mode script.sh\n'
        'echo "rc=$?"'
    )

    assert rc_of(result) == 0
    assert "/etc/systemd/system/unit.service" in result.stdout
    assert "644" in result.stdout


def test_load_file_map_skips_blank_lines(tmp_path: Path) -> None:
    map_file = write_map(tmp_path, "a.conf|/etc/a.conf|600\n\n\nb.conf|/etc/b.conf|644\n")

    result = run_utils(
        f'load_file_map "{map_file}"; echo "count=${{#FILE_MAP_NAMES[@]}}"; echo "rc=$?"'
    )

    assert rc_of(result) == 0
    assert "count=2" in result.stdout


def test_load_file_map_fails_loudly_on_a_missing_map(tmp_path: Path) -> None:
    result = run_utils(f'load_file_map "{tmp_path}/absent"; echo "rc=$?"')

    assert rc_of(result) == 1
    assert "missing file" in result.stderr


def test_mapped_dest_rejects_an_unknown_entry(tmp_path: Path) -> None:
    map_file = write_map(tmp_path, "a.conf|/etc/a.conf|600\n")

    result = run_utils(f'load_file_map "{map_file}"; mapped_dest ghost.conf; echo "rc=$?"')

    assert rc_of(result) == 1
    assert "missing file-map entry: ghost.conf" in result.stderr


def test_python_written_file_map_round_trips_through_bash(tmp_path: Path) -> None:
    """Cross-language contract test.

    `module_support.write_file_map` is the only writer and `load_file_map` the only
    reader, but they share no code and no schema. A delimiter or column-order change
    on either side breaks every module at deploy time; nothing else catches it.
    """
    specs = (
        FileSpec("homelab-thing.service", "/etc/systemd/system/homelab-thing.service"),
        FileSpec("thing.sh", "/usr/local/bin/thing.sh", mode="755"),
        FileSpec("thing.env", "/etc/homelab/thing.env", mode="600"),
    )
    write_file_map(tmp_path, specs)

    result = run_utils(
        f'load_file_map "{tmp_path}/file-map.conf"\n'
        'for name in "${FILE_MAP_NAMES[@]}"; do\n'
        '    echo "entry=$name|$(mapped_dest "$name")|$(mapped_mode "$name")"\n'
        'done\n'
        'echo "rc=$?"'
    )

    assert rc_of(result) == 0
    entries = [
        line.removeprefix("entry=")
        for line in result.stdout.splitlines()
        if line.startswith("entry=")
    ]
    assert entries == [f"{s.build_name}|{s.remote_path}|{s.mode}" for s in specs]


# --------------------------------------------------------------------------
# install_file_map / install_build_file_validated
# --------------------------------------------------------------------------


def stage_map(tmp_path: Path, entries: dict[str, tuple[str, str]]) -> tuple[Path, Path]:
    """Build a BUILD_DIR plus file-map. entries: build_name -> (content, mode)."""
    build_dir = tmp_path / "build"
    dest_dir = tmp_path / "dest"
    build_dir.mkdir()
    dest_dir.mkdir()
    lines = []
    for name, (content, mode) in entries.items():
        (build_dir / name).write_text(content, encoding="utf-8")
        lines.append(f"{name}|{dest_dir / name}|{mode}")
    (build_dir / "file-map.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return build_dir, dest_dir


def test_install_file_map_reports_changed_when_any_entry_changed(tmp_path: Path) -> None:
    build_dir, dest_dir = stage_map(
        tmp_path, {"a.conf": ("a\n", "644"), "b.conf": ("b\n", "600")}
    )

    result = run_utils(
        f'load_file_map "{build_dir}/file-map.conf"\n'
        f'install_file_map "{build_dir}"; echo "rc=$?"'
    )

    assert rc_of(result) == 0
    assert (dest_dir / "a.conf").read_text(encoding="utf-8") == "a\n"
    assert oct((dest_dir / "b.conf").stat().st_mode & 0o777) == "0o600"


def test_install_file_map_reports_unchanged_on_a_second_run(tmp_path: Path) -> None:
    """rc=1 is what every installer feeds into homelab_reload_and_clear_failed as
    its `changed` flag. If a no-op deploy reported 0, the failed-unit gate would
    fire on every run and defeat its own purpose."""
    build_dir, _ = stage_map(
        tmp_path, {"a.conf": ("a\n", "644"), "b.conf": ("b\n", "600")}
    )
    snippet = (
        f'load_file_map "{build_dir}/file-map.conf"\n'
        f'install_file_map "{build_dir}" >/dev/null\n'
        f'install_file_map "{build_dir}" >/dev/null; echo "rc=$?"'
    )

    result = run_utils(snippet)

    assert rc_of(result) == 1


def test_install_file_map_requires_a_build_dir(tmp_path: Path) -> None:
    result = run_utils('FILE_MAP_NAMES=(); install_file_map ""; echo "rc=$?"')

    assert rc_of(result) == 2
    assert "BUILD_DIR is required" in result.stderr


def test_validated_install_keeps_the_file_when_validation_passes(tmp_path: Path) -> None:
    build_dir, dest_dir = stage_map(tmp_path, {"sshd.conf": ("PermitRootLogin no\n", "600")})

    result = run_utils(
        f'load_file_map "{build_dir}/file-map.conf"\n'
        f'BUILD_DIR="{build_dir}"\n'
        'install_build_file_validated sshd.conf true; echo "rc=$?"'
    )

    assert rc_of(result) == 0
    assert (dest_dir / "sshd.conf").read_text(encoding="utf-8") == "PermitRootLogin no\n"


def test_validated_install_restores_previous_contents_on_failure(tmp_path: Path) -> None:
    """The reason this helper exists: an sshd drop-in can only be validated after
    it is in place. A failed validation must leave the *old, working* config."""
    build_dir, dest_dir = stage_map(tmp_path, {"sshd.conf": ("Broken !!\n", "600")})
    (dest_dir / "sshd.conf").write_text("PermitRootLogin no\n", encoding="utf-8")

    result = run_utils(
        f'load_file_map "{build_dir}/file-map.conf"\n'
        f'BUILD_DIR="{build_dir}"\n'
        'install_build_file_validated sshd.conf false; echo "rc=$?"'
    )

    assert rc_of(result) == 2
    assert (dest_dir / "sshd.conf").read_text(encoding="utf-8") == "PermitRootLogin no\n"
    assert "rolling back" in result.stderr


def test_validated_install_removes_a_new_file_on_failure(tmp_path: Path) -> None:
    """With no prior file there is nothing to restore, so the drop-in must be
    deleted outright — leaving it would keep the broken config active."""
    build_dir, dest_dir = stage_map(tmp_path, {"sshd.conf": ("Broken !!\n", "600")})

    result = run_utils(
        f'load_file_map "{build_dir}/file-map.conf"\n'
        f'BUILD_DIR="{build_dir}"\n'
        'install_build_file_validated sshd.conf false; echo "rc=$?"'
    )

    assert rc_of(result) == 2
    assert not (dest_dir / "sshd.conf").exists()


def test_validated_install_skips_validation_when_unchanged(tmp_path: Path) -> None:
    """rc=1 (unchanged) must short-circuit before the validation command runs;
    re-validating an untouched file is wasted work and, for `sshd -t`, noise."""
    build_dir, dest_dir = stage_map(tmp_path, {"sshd.conf": ("PermitRootLogin no\n", "600")})
    (dest_dir / "sshd.conf").write_text("PermitRootLogin no\n", encoding="utf-8")
    marker = tmp_path / "validated"

    result = run_utils(
        f'load_file_map "{build_dir}/file-map.conf"\n'
        f'BUILD_DIR="{build_dir}"\n'
        f'install_build_file_validated sshd.conf touch "{marker}"; echo "rc=$?"'
    )

    assert rc_of(result) == 1
    assert not marker.exists()


# --------------------------------------------------------------------------
# require_env / require_file / require_dir
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("FLAG_A=true; FLAG_B=false", 0),
        ("FLAG_A=true; FLAG_B=''", 1),
        ("FLAG_A=true; unset FLAG_B", 1),
    ],
)
def test_require_env_rejects_empty_and_unset(setup: str, expected: int) -> None:
    """An empty flag is the dangerous case, not a missing one.

    `ensure_timer_state` treats anything != "true" as "disable", so a truncated env
    file silently disables snapshots and replication rather than erroring.
    """
    result = run_utils(f'{setup}; require_env FLAG_A FLAG_B; echo "rc=$?"')

    assert rc_of(result) == expected


def test_require_env_names_every_missing_value() -> None:
    result = run_utils('unset A B; require_env A B; echo "rc=$?"')

    assert rc_of(result) == 1
    assert "A B" in result.stderr


def test_require_dir_and_require_file_use_the_label_in_errors(tmp_path: Path) -> None:
    result = run_utils(
        f'require_dir "{tmp_path}/absent" "the staging dir"; echo "rc=$?"'
    )

    assert rc_of(result) == 1
    assert "missing directory: the staging dir" in result.stderr


def test_require_file_rejects_a_directory(tmp_path: Path) -> None:
    """`-f`, not `-e`: a directory where a file is expected is a staging bug."""
    result = run_utils(f'require_file "{tmp_path}" "build artifact"; echo "rc=$?"')

    assert rc_of(result) == 1


# --------------------------------------------------------------------------
# ensure_timer_state
# --------------------------------------------------------------------------


def timer_log(tmp_path: Path) -> Path:
    log = tmp_path / "systemctl.log"
    log.touch()
    return log


def test_ensure_timer_state_disables_when_flag_is_not_true(tmp_path: Path) -> None:
    log = timer_log(tmp_path)

    run_utils(
        "ensure_timer_state homelab-test.timer false false",
        env={"SYSTEMCTL_LOG": str(log), "SYSTEMCTL_IS_ENABLED": "enabled"},
        stub_systemctl=True,
    )

    assert "disable --now homelab-test.timer" in log.read_text(encoding="utf-8")


def test_ensure_timer_state_does_not_disable_an_already_disabled_timer(
    tmp_path: Path,
) -> None:
    log = timer_log(tmp_path)

    run_utils(
        "ensure_timer_state homelab-test.timer false false",
        env={"SYSTEMCTL_LOG": str(log), "SYSTEMCTL_IS_ENABLED": "disabled"},
        stub_systemctl=True,
    )

    assert "disable" not in log.read_text(encoding="utf-8")


def test_ensure_timer_state_treats_a_blank_flag_as_disable(tmp_path: Path) -> None:
    """The failure mode require_env exists to prevent: an unrendered flag must
    fall to the safe side rather than being read as enabled."""
    log = timer_log(tmp_path)

    run_utils(
        'ensure_timer_state homelab-test.timer "" false',
        env={"SYSTEMCTL_LOG": str(log), "SYSTEMCTL_IS_ENABLED": "enabled"},
        stub_systemctl=True,
    )

    assert "disable --now homelab-test.timer" in log.read_text(encoding="utf-8")


def test_ensure_timer_state_enables_a_disabled_timer(tmp_path: Path) -> None:
    log = timer_log(tmp_path)

    run_utils(
        "ensure_timer_state homelab-test.timer true false",
        env={"SYSTEMCTL_LOG": str(log), "SYSTEMCTL_IS_ENABLED": "disabled"},
        stub_systemctl=True,
    )

    assert "enable --now homelab-test.timer" in log.read_text(encoding="utf-8")


def test_ensure_timer_state_restarts_only_when_units_changed(tmp_path: Path) -> None:
    log = timer_log(tmp_path)

    run_utils(
        "ensure_timer_state homelab-test.timer true true",
        env={"SYSTEMCTL_LOG": str(log), "SYSTEMCTL_IS_ENABLED": "enabled"},
        stub_systemctl=True,
    )

    assert "restart homelab-test.timer" in log.read_text(encoding="utf-8")


def test_ensure_timer_state_is_inert_on_an_unchanged_enabled_timer(
    tmp_path: Path,
) -> None:
    log = timer_log(tmp_path)

    run_utils(
        "ensure_timer_state homelab-test.timer true false",
        env={"SYSTEMCTL_LOG": str(log), "SYSTEMCTL_IS_ENABLED": "enabled"},
        stub_systemctl=True,
    )

    log_text = log.read_text(encoding="utf-8")
    assert "restart" not in log_text
    assert "enable --now" not in log_text
