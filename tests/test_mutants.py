"""Tests for the mutation-score reader and its ratchet.

Written assertion-first on purpose: this module exists because coverage records
execution rather than assertion, and a test suite for it that only executed it
would be the exact failure it is meant to detect.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from homelab import cli, mutants

ROOT = Path(__file__).resolve().parents[1]

KILLED = 1
SURVIVED = 0
NO_TESTS = 33
TIMEOUT = 36
NOT_RUN = None

# An import of the package through the `src.` path rather than its installed name.
SRC_IMPORT = re.compile(r"^\s*(?:from|import)\s+src\.homelab", re.MULTILINE)


def write_meta(mutants_dir: Path, filename: str, codes: dict[str, int | None]) -> Path:
    """Write a `<file>.py.meta` sidecar in mutmut's own shape."""
    path = mutants_dir / f"{filename}.meta"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code_by_key": codes}), encoding="utf-8")
    return path


def score(killed: int = 0, survived: int = 0, no_tests: int = 0, name: str = "a.py"):
    return mutants.FileScore(filename=name, killed=killed, survived=survived, no_tests=no_tests)


class TestClassify:
    def test_buckets_the_three_outcomes(self) -> None:
        assert mutants.classify([KILLED, SURVIVED, SURVIVED, NO_TESTS]) == (1, 2, 1)

    def test_timeout_counts_as_killed(self) -> None:
        # The mutant made the suite hang; the suite did surface that.
        assert mutants.classify([TIMEOUT]) == (1, 0, 0)

    def test_segfault_counts_as_killed(self) -> None:
        assert mutants.classify([-11]) == (1, 0, 0)

    def test_unrun_mutants_are_dropped_not_guessed(self) -> None:
        # A partial sweep must report only what it measured. Counting `not checked`
        # as either killed or survived would invent a verdict.
        assert mutants.classify([NOT_RUN, NOT_RUN, 34, 2]) == (0, 0, 0)

    def test_empty_input_is_all_zero(self) -> None:
        assert mutants.classify([]) == (0, 0, 0)


class TestFileScore:
    def test_undetected_sums_survived_and_untested(self) -> None:
        assert score(killed=1, survived=2, no_tests=3).undetected == 5

    def test_score_is_percentage_detected(self) -> None:
        assert score(killed=3, survived=1).score == 75.0

    def test_untested_mutants_lower_the_score(self) -> None:
        # The distinction matters for the report, never for the arithmetic.
        assert score(killed=3, no_tests=1).score == 75.0

    def test_empty_file_scores_one_hundred_rather_than_dividing_by_zero(self) -> None:
        assert score().score == 100.0
        assert score().total == 0

    def test_format_shows_every_bucket(self) -> None:
        line = score(killed=7, survived=2, no_tests=1, name="src/x.py").format()
        assert "70.0%" in line
        assert "7 killed" in line
        assert "2 survived" in line
        assert "1 untested" in line
        assert "src/x.py" in line


class TestReadResults:
    def test_reads_mutmut_metadata_into_scores(self, tmp_path: Path) -> None:
        write_meta(tmp_path, "src/homelab/crap.py", {"a": KILLED, "b": SURVIVED})

        results = mutants.read_results(tmp_path)

        assert [result.filename for result in results] == ["src/homelab/crap.py"]
        assert results[0].killed == 1
        assert results[0].survived == 1

    def test_files_with_nothing_run_are_omitted_entirely(self, tmp_path: Path) -> None:
        # A scoped sweep leaves ~40 untouched files behind. Reporting them as a
        # perfect 100% would be a lie of omission.
        write_meta(tmp_path, "src/homelab/crap.py", {"a": KILLED})
        write_meta(tmp_path, "src/homelab/hosts.py", {"a": NOT_RUN, "b": NOT_RUN})

        assert [result.filename for result in mutants.read_results(tmp_path)] == [
            "src/homelab/crap.py"
        ]

    def test_worst_score_sorts_first(self, tmp_path: Path) -> None:
        write_meta(tmp_path, "good.py", {"a": KILLED, "b": KILLED})
        write_meta(tmp_path, "bad.py", {"a": SURVIVED, "b": KILLED})

        assert [result.filename for result in mutants.read_results(tmp_path)] == [
            "bad.py",
            "good.py",
        ]

    def test_missing_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert mutants.read_results(tmp_path / "absent") == []

    def test_meta_without_results_key_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "x.py.meta"
        path.write_text(json.dumps({"hash_by_function_name": {}}), encoding="utf-8")
        assert mutants.read_results(tmp_path) == []


class TestBaselineRatchet:
    def test_undetected_mutants_in_an_unlisted_file_fail(self) -> None:
        verdict = mutants.check_baseline([score(killed=1, survived=1)], {})

        assert verdict.failed
        assert [row.filename for row in verdict.new] == ["a.py"]

    def test_a_clean_unlisted_file_passes(self) -> None:
        verdict = mutants.check_baseline([score(killed=4)], {})

        assert not verdict.failed
        assert verdict.new == []

    def test_a_grandfathered_file_at_its_recorded_count_passes(self) -> None:
        verdict = mutants.check_baseline([score(killed=1, survived=3)], {"a.py": 3})

        assert not verdict.failed
        assert verdict.cleared == []

    def test_a_grandfathered_file_that_got_worse_fails(self) -> None:
        verdict = mutants.check_baseline([score(killed=1, survived=4)], {"a.py": 3})

        assert verdict.failed
        assert verdict.regressed[0][1] == 3

    def test_one_extra_undetected_mutant_is_enough_to_fail(self) -> None:
        # Unlike coverage, mutation results are deterministic, so there is no
        # noise tolerance to hide behind here.
        assert mutants.check_baseline([score(survived=4)], {"a.py": 3}).failed

    def test_an_improvement_is_reported_as_cleared_not_as_a_pass(self) -> None:
        verdict = mutants.check_baseline([score(killed=2, survived=1)], {"a.py": 3})

        assert not verdict.failed
        assert verdict.cleared == [("a.py", 3, 1)]

    def test_baseline_entries_for_unswept_files_are_left_alone(self) -> None:
        # A scoped run must not report every other baselined file as cleared.
        verdict = mutants.check_baseline([score(killed=1, name="a.py")], {"b.py": 2})

        assert not verdict.failed
        assert verdict.cleared == []


class TestBaselineFile:
    def test_round_trips_undetected_counts(self, tmp_path: Path) -> None:
        path = tmp_path / mutants.BASELINE_FILENAME
        mutants.write_baseline(path, [score(killed=1, survived=2, no_tests=1)])

        assert mutants.load_baseline(path) == {"a.py": 3}

    def test_hand_written_notes_survive_a_rewrite(self, tmp_path: Path) -> None:
        """`_`-prefixed keys record how the figures were measured. Dropping them on
        every --update-baseline left a "restore it afterwards" step nobody remembers."""
        path = tmp_path / mutants.BASELINE_FILENAME
        path.write_text(
            json.dumps({"_measured": "serial only", "files": {"a.py": 9}}),
            encoding="utf-8",
        )

        mutants.write_baseline(path, [score(killed=1, survived=2)])

        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["_measured"] == "serial only"
        assert written["files"] == {"a.py": 2}
        assert list(written) == ["_comment", "_measured", "files"]

    def test_the_generated_comment_is_not_treated_as_a_hand_written_note(
        self, tmp_path: Path
    ) -> None:
        """Otherwise a stale _comment would be preserved forever instead of refreshed."""
        path = tmp_path / mutants.BASELINE_FILENAME
        path.write_text(
            json.dumps({"_comment": "outdated wording", "files": {}}), encoding="utf-8"
        )

        mutants.write_baseline(path, [score(killed=1, survived=1)])

        assert "outdated wording" not in path.read_text(encoding="utf-8")

    def test_clean_files_are_not_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / mutants.BASELINE_FILENAME
        mutants.write_baseline(path, [score(killed=5)])

        assert mutants.load_baseline(path) == {}

    def test_a_scoped_rewrite_preserves_untouched_entries(self, tmp_path: Path) -> None:
        # Otherwise `homelab mutants homelab.crap.* --update-baseline` would
        # silently amnesty every file that run did not touch.
        path = tmp_path / mutants.BASELINE_FILENAME
        first = [score(survived=2, name="a.py"), score(survived=9, name="b.py")]
        mutants.write_baseline(path, first)
        mutants.write_baseline(path, [score(survived=1, name="a.py")])

        assert mutants.load_baseline(path) == {"a.py": 1, "b.py": 9}

    def test_a_swept_file_that_became_clean_is_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / mutants.BASELINE_FILENAME
        mutants.write_baseline(path, [score(survived=2)])
        mutants.write_baseline(path, [score(killed=2)])

        assert mutants.load_baseline(path) == {}

    def test_missing_baseline_exempts_nothing(self, tmp_path: Path) -> None:
        assert mutants.load_baseline(tmp_path / "absent.json") == {}

    def test_written_file_states_the_ratchet_rule(self, tmp_path: Path) -> None:
        path = tmp_path / mutants.BASELINE_FILENAME
        mutants.write_baseline(path, [score(survived=1)])

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "never added or raised by hand" in payload["_comment"]
        assert "only_mutate" in payload["_comment"]


class TestSummarize:
    def test_rolls_every_bucket_up(self) -> None:
        total = mutants.summarize([score(killed=1, survived=2), score(killed=3, no_tests=4)])

        assert (total.killed, total.survived, total.no_tests) == (4, 2, 4)
        assert total.filename == "2 file(s)"


class TestExitCodeTableMatchesMutmut:
    """Guard the copied status table against upstream drift."""

    DETECTED_STATUSES = {"killed", "timeout", "segfault", "caught by type check"}

    def test_table_agrees_with_installed_mutmut(self) -> None:
        mutmut_main = pytest.importorskip("mutmut.__main__")
        table = mutmut_main.status_by_exit_code

        for code in mutants.DETECTED_EXIT_CODES:
            assert table[code] in self.DETECTED_STATUSES, code
        assert table[mutants.SURVIVED_EXIT_CODE] == "survived"
        for code in mutants.NO_TEST_EXIT_CODES:
            assert table[code] == "no tests", code

    def test_every_upstream_kill_like_code_is_covered(self) -> None:
        mutmut_main = pytest.importorskip("mutmut.__main__")

        expected = {
            code
            for code, status in mutmut_main.status_by_exit_code.items()
            if status in self.DETECTED_STATUSES
        }
        assert expected - mutants.DETECTED_EXIT_CODES == set()


class TestMutmutConfig:
    """The sweep runs from a copied tree; what is not copied is not importable."""

    @staticmethod
    def config() -> dict:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return data["tool"]["mutmut"]

    def test_also_copy_covers_every_payload_directory(self) -> None:
        # Tests resolve ROOT as `tests/..`, which under mutmut is `mutants/`. A
        # module directory missing from also_copy makes its tests fail for a
        # reason that has nothing to do with the mutant under test.
        also_copy = set(self.config()["also_copy"])
        payload_dirs = {
            path.name
            for path in ROOT.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and path.name not in {"src", "tests"}
            and any((path / child).is_dir() for child in ("scripts", "templates", "configs"))
        }

        assert payload_dirs - also_copy == set()

    def test_hosts_conf_is_copied(self) -> None:
        # test_render_golden.py renders against the real inventory.
        assert "hosts.conf" in self.config()["also_copy"]

    def test_whole_package_is_walked_but_only_the_core_is_mutated(self) -> None:
        # Narrowing source_paths broke `homelab.modules.*` imports outright:
        # unwalked files are absent from the copy rather than copied verbatim.
        config = self.config()
        assert config["source_paths"] == ["src/homelab"]
        assert config["only_mutate"]

    def test_scoped_files_exist(self) -> None:
        # A renamed or split module would otherwise silently drop out of scope
        # and take its baseline entry with it.
        for pattern in self.config()["only_mutate"]:
            assert (ROOT / pattern).is_file(), pattern

    def test_deprecated_config_keys_are_not_used(self) -> None:
        # mutmut 3.8 renamed these; leaving them in place only emits a warning,
        # so nothing else would catch the drift.
        config = self.config()
        assert "paths_to_mutate" not in config
        assert "tests_dir" not in config

    def test_no_test_imports_the_package_through_src(self) -> None:
        # mutmut asserts hard against `src.`-prefixed module names, and importing
        # the same module under two names duplicates its module-level state.
        offenders = [
            path.name
            for path in sorted((ROOT / "tests").glob("*.py"))
            if SRC_IMPORT.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []


class TestCliReport:
    def test_scores_an_existing_tree_without_sweeping(self, tmp_path: Path, capsys) -> None:
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": KILLED})

        exit_code = cli.run_mutation_report(tmp_path, (), False, 20, 8, sweep=False)

        assert exit_code == 0
        assert "100.0%" in capsys.readouterr().out

    def test_empty_tree_reports_failure_rather_than_a_clean_gate(self, tmp_path: Path) -> None:
        assert cli.run_mutation_report(tmp_path, (), False, 20, 8, sweep=False) == 1

    def test_undetected_mutants_fail_the_gate(self, tmp_path: Path) -> None:
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": SURVIVED})

        assert cli.run_mutation_report(tmp_path, (), False, 20, 8, sweep=False) == 1

    def test_baselined_survivors_pass(self, tmp_path: Path) -> None:
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": SURVIVED})
        mutants.write_baseline(tmp_path / mutants.BASELINE_FILENAME, [score(survived=1)])

        assert cli.run_mutation_report(tmp_path, (), False, 20, 8, sweep=False) == 0

    def test_improvement_is_surfaced_as_a_warning(self, tmp_path: Path, capsys) -> None:
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": SURVIVED, "y": KILLED})
        mutants.write_baseline(tmp_path / mutants.BASELINE_FILENAME, [score(survived=5)])

        exit_code = cli.run_mutation_report(tmp_path, (), False, 20, 8, sweep=False)

        assert exit_code == 0
        assert "cleared: a.py 5 -> 1" in capsys.readouterr().out

    def test_update_baseline_writes_and_skips_the_gate(self, tmp_path: Path) -> None:
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": SURVIVED})

        exit_code = cli.run_mutation_report(tmp_path, (), True, 20, 1, sweep=False)

        assert exit_code == 0
        assert mutants.load_baseline(tmp_path / mutants.BASELINE_FILENAME) == {"a.py": 1}

    def test_update_baseline_refuses_parallel_results(self, tmp_path: Path, capsys) -> None:
        """Children share one tree, so parallel figures are biased low by false kills."""
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": SURVIVED})

        exit_code = cli.run_mutation_report(tmp_path, (), True, 20, 8, sweep=False)

        assert exit_code == 1
        assert "--update-baseline needs --max-children 1" in capsys.readouterr().out
        assert not (tmp_path / mutants.BASELINE_FILENAME).exists()

    def test_parallel_sweep_warns_that_results_are_optimistic(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        monkeypatch.setattr(cli, "run_mutmut", lambda *args: None)
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": KILLED})

        cli.run_mutation_report(tmp_path, (), False, 20, 8, sweep=True)

        assert "exploration-only" in capsys.readouterr().out

    def test_serial_sweep_does_not_warn(self, monkeypatch, tmp_path: Path, capsys) -> None:
        monkeypatch.setattr(cli, "run_mutmut", lambda *args: None)
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": KILLED})

        cli.run_mutation_report(tmp_path, (), False, 20, 1, sweep=True)

        assert "exploration-only" not in capsys.readouterr().out

    def test_sweep_invokes_mutmut_with_the_requested_targets(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        calls: list[tuple] = []
        monkeypatch.setattr(cli, "run_mutmut", lambda *args: calls.append(args))
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": KILLED})

        cli.run_mutation_report(tmp_path, ("homelab.hosts.*",), False, 20, 4, sweep=True)

        assert calls == [(tmp_path, ("homelab.hosts.*",), 4)]

    def test_failure_message_names_the_repair_path(self) -> None:
        verdict = mutants.check_baseline([score(survived=2)], {})

        message = cli.mutation_failure(verdict)
        assert "NEW" in message
        assert "mutmut show" in message
        assert "may only shrink" in message

    def test_failure_message_shows_the_baseline_it_regressed_from(self) -> None:
        verdict = mutants.check_baseline([score(survived=4)], {"a.py": 3})

        assert "(baseline 3)" in cli.mutation_failure(verdict)


class TestMutmutInvocation:
    def test_sweep_is_offline_and_uncovered(self, monkeypatch) -> None:
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        monkeypatch.setenv("HOMELAB_OFFLINE", "0")

        env = cli.mutmut_env()

        assert env["HOMELAB_OFFLINE"] == "1"
        assert "--no-cov" in env["PYTEST_ADDOPTS"]

    def test_pythonpath_is_dropped_so_real_source_cannot_shadow_the_mutants(
        self, monkeypatch
    ) -> None:
        # The ./validate and ./deploy launchers set PYTHONPATH to the real src.
        # Leaving it set makes every mutant look like a survivor.
        monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))

        assert "PYTHONPATH" not in cli.mutmut_env()

    def test_existing_addopts_are_preserved(self, monkeypatch) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "-k something")

        assert cli.mutmut_env()["PYTEST_ADDOPTS"].startswith("-k something")

    def test_command_passes_targets_and_parallelism(self, monkeypatch, tmp_path: Path) -> None:
        seen: dict = {}

        class Result:
            returncode = 0

        def fake_run(command, cwd, env, check):
            seen["command"] = command
            return Result()

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        cli.run_mutmut(tmp_path, ("homelab.crap.*",), 6)

        assert seen["command"][1:5] == ["-m", "mutmut", "run", "--max-children"]
        assert seen["command"][5] == "6"
        assert seen["command"][-1] == "homelab.crap.*"

    def test_a_tooling_failure_is_not_reported_as_a_verdict(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        class Result:
            returncode = 2

        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Result())

        with pytest.raises(Exception, match="mutmut run failed"):
            cli.run_mutmut(tmp_path, (), 8)


class TestCliCommand:
    def test_missing_mutmut_is_a_clear_error_not_a_crash(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "_module_available", lambda name: False)

        result = CliRunner().invoke(cli.main, ["mutants"])

        assert result.exit_code != 0
        assert "pip install" in result.output

    def test_no_run_does_not_require_mutmut(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cli, "_module_available", lambda name: False)
        monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": KILLED})

        result = CliRunner().invoke(cli.main, ["mutants", "--no-run"])

        assert result.exit_code == 0

    def test_scope_is_left_to_the_mutmut_config(self, monkeypatch, tmp_path: Path) -> None:
        # No default target list here: pyproject's only_mutate owns the scope,
        # and a second copy of it would be free to drift.
        seen: list[tuple] = []
        monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(cli, "_module_available", lambda name: True)
        monkeypatch.setattr(cli, "run_mutation_report", lambda *args: seen.append(args) or 0)

        CliRunner().invoke(cli.main, ["mutants"])

        assert seen[0][1] == ()


class TestSuiteFingerprint:
    """The staleness check mutmut does not do for itself.

    mutmut 3.8 invalidates cached verdicts on source-function hashes, its own config
    fingerprint, and tracked *non*-Python files. A test-only edit matches none of
    those, so without this the sweep reprints the previous run's numbers. That is the
    one change this whole workflow is built around, so it gets its own tests.
    """

    def test_identical_content_fingerprints_identically(self, tmp_path: Path) -> None:
        first = tmp_path / "test_a.py"
        first.write_text("assert True\n", encoding="utf-8")
        second = tmp_path / "copy" / "test_a.py"
        second.parent.mkdir()
        second.write_text("assert True\n", encoding="utf-8")

        assert mutants.suite_fingerprint([first]) == mutants.suite_fingerprint([first])
        # Different path, same bytes: the path is part of the hash, so these differ.
        assert mutants.suite_fingerprint([first]) != mutants.suite_fingerprint([second])

    def test_a_changed_assertion_changes_the_fingerprint(self, tmp_path: Path) -> None:
        path = tmp_path / "test_a.py"
        path.write_text("assert x == 1\n", encoding="utf-8")
        before = mutants.suite_fingerprint([path])

        path.write_text("assert x == 2\n", encoding="utf-8")

        assert mutants.suite_fingerprint([path]) != before

    def test_a_new_test_file_changes_the_fingerprint(self, tmp_path: Path) -> None:
        first = tmp_path / "test_a.py"
        first.write_text("assert True\n", encoding="utf-8")
        second = tmp_path / "test_b.py"
        second.write_text("assert True\n", encoding="utf-8")

        assert mutants.suite_fingerprint([first, second]) != mutants.suite_fingerprint([first])

    def test_ordering_does_not_matter(self, tmp_path: Path) -> None:
        first = tmp_path / "test_a.py"
        first.write_text("a\n", encoding="utf-8")
        second = tmp_path / "test_b.py"
        second.write_text("b\n", encoding="utf-8")

        assert mutants.suite_fingerprint([second, first]) == mutants.suite_fingerprint(
            [first, second]
        )

    def test_an_unreadable_file_does_not_raise(self, tmp_path: Path) -> None:
        assert mutants.suite_fingerprint([tmp_path / "gone.py"])

    def test_no_recorded_fingerprint_is_not_staleness(self, tmp_path: Path) -> None:
        # First sweep, or a tree from before this check existed. Neither is evidence.
        assert mutants.suite_changed(tmp_path, "abc") is False

    def test_a_matching_fingerprint_is_not_staleness(self, tmp_path: Path) -> None:
        mutants.write_suite_fingerprint(tmp_path, "abc")

        assert mutants.suite_changed(tmp_path, "abc") is False

    def test_a_differing_fingerprint_is_staleness(self, tmp_path: Path) -> None:
        mutants.write_suite_fingerprint(tmp_path, "abc")

        assert mutants.suite_changed(tmp_path, "def") is True

    def test_fingerprint_round_trips(self, tmp_path: Path) -> None:
        mutants.write_suite_fingerprint(tmp_path / "mutants", "abc")

        assert mutants.read_suite_fingerprint(tmp_path / "mutants") == "abc"

    def test_an_empty_fingerprint_file_reads_as_absent(self, tmp_path: Path) -> None:
        (tmp_path / mutants.SUITE_FINGERPRINT_FILENAME).write_text("", encoding="utf-8")

        assert mutants.read_suite_fingerprint(tmp_path) is None

    def test_discard_results_removes_the_tree(self, tmp_path: Path) -> None:
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": KILLED})

        mutants.discard_results(tmp_path / mutants.MUTANTS_DIRNAME)

        assert not (tmp_path / mutants.MUTANTS_DIRNAME).exists()

    def test_discarding_a_missing_tree_is_not_an_error(self, tmp_path: Path) -> None:
        mutants.discard_results(tmp_path / "nope")


class TestStaleResultHandling:
    @staticmethod
    def _suite(root: Path, body: str) -> None:
        path = root / "tests" / "test_thing.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_test_suite_paths_include_conftest(self) -> None:
        names = {path.name for path in cli.test_suite_paths(ROOT)}

        assert "conftest.py" in names
        assert "test_mutants.py" in names

    def test_a_changed_test_discards_the_cached_tree(self, tmp_path: Path) -> None:
        self._suite(tmp_path, "assert 1\n")
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": SURVIVED})
        mutants.write_suite_fingerprint(tmp_path / mutants.MUTANTS_DIRNAME, "stale")

        cli.drop_stale_results(tmp_path, ())

        assert not (tmp_path / mutants.MUTANTS_DIRNAME).exists()

    def test_an_unchanged_suite_keeps_the_cached_tree(self, tmp_path: Path) -> None:
        self._suite(tmp_path, "assert 1\n")
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": SURVIVED})
        fingerprint = mutants.suite_fingerprint(cli.test_suite_paths(tmp_path))
        mutants.write_suite_fingerprint(tmp_path / mutants.MUTANTS_DIRNAME, fingerprint)

        assert cli.drop_stale_results(tmp_path, ()) == fingerprint
        assert (tmp_path / mutants.MUTANTS_DIRNAME / "a.py.meta").is_file()

    def test_a_narrowed_run_warns_instead_of_wiping(self, tmp_path: Path, capsys) -> None:
        """Wiping would drop every untargeted file's results from the report."""
        self._suite(tmp_path, "assert 1\n")
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": SURVIVED})
        mutants.write_suite_fingerprint(tmp_path / mutants.MUTANTS_DIRNAME, "stale")

        cli.drop_stale_results(tmp_path, ("homelab.hosts.*",))

        assert (tmp_path / mutants.MUTANTS_DIRNAME / "a.py.meta").is_file()
        assert "stale" in capsys.readouterr().out

    def test_a_sweep_records_the_fingerprint_it_ran_against(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        self._suite(tmp_path, "assert 1\n")
        monkeypatch.setattr(
            cli,
            "run_mutmut",
            lambda root, *_args: write_meta(root / mutants.MUTANTS_DIRNAME, "a.py", {"x": KILLED}),
        )

        cli.run_mutation_report(tmp_path, (), False, 20, 4, sweep=True)

        assert mutants.read_suite_fingerprint(tmp_path / mutants.MUTANTS_DIRNAME) == (
            mutants.suite_fingerprint(cli.test_suite_paths(tmp_path))
        )

    def test_scoring_without_a_sweep_leaves_the_fingerprint_alone(self, tmp_path: Path) -> None:
        """`--no-run` reports the old tree as-is; it must not claim to be current."""
        write_meta(tmp_path / mutants.MUTANTS_DIRNAME, "a.py", {"x": KILLED})

        cli.run_mutation_report(tmp_path, (), False, 20, 4, sweep=False)

        assert mutants.read_suite_fingerprint(tmp_path / mutants.MUTANTS_DIRNAME) is None


# A mutated source file in mutmut's own shape: an unmutated `__mutmut_orig`
# beside numbered variants, closed by the dispatch table it appends.
MUTATED_SOURCE = '''
def x_is_fresh__mutmut_orig(ttl):
    if ttl <= 0:
        return False
    return True

def x_is_fresh__mutmut_1(ttl):
    if ttl < 0:
        return False
    return True

def x_is_fresh__mutmut_2(ttl):
    if ttl <= 1:
        return False
    return True

x_is_fresh__mutmut_mutants = {"x_is_fresh__mutmut_1": x_is_fresh__mutmut_1}

def x_other__mutmut_orig(name):
    return name.strip()

def x_other__mutmut_1(name):
    return name.rstrip()
'''


def write_scored_file(mutants_dir: Path, filename: str, source: str, codes: dict[str, int]) -> None:
    """Write both halves of a scored file: the mutated source and its sidecar."""
    write_meta(mutants_dir, filename, codes)
    (mutants_dir / filename).write_text(source, encoding="utf-8")


class TestFunctionOf:
    @pytest.mark.parametrize(
        ("mutant", "expected"),
        [
            ("x__is_cache_fresh__mutmut_7", "_is_cache_fresh"),
            ("x_load_catalog__mutmut_orig", "load_catalog"),
            ("x_load_catalog__mutmut_120", "load_catalog"),
        ],
    )
    def test_strips_the_wrapper_naming(self, mutant: str, expected: str) -> None:
        """Both affixes have to go, and the leading `x_` must not eat a real
        underscore-prefixed private name."""
        assert mutants.function_of(mutant) == expected


class TestMutantBodies:
    def test_indexes_every_variant_including_the_original(self) -> None:
        bodies = mutants.mutant_bodies(MUTATED_SOURCE)

        assert set(bodies) == {
            "x_is_fresh__mutmut_orig",
            "x_is_fresh__mutmut_1",
            "x_is_fresh__mutmut_2",
            "x_other__mutmut_orig",
            "x_other__mutmut_1",
        }

    def test_a_body_stops_at_the_next_definition(self) -> None:
        """Bodies are compared line-for-line, so bleeding into the next function
        would diff two unrelated blocks and report a mutant that changed nothing."""
        assert mutants.mutant_bodies(MUTATED_SOURCE)["x_other__mutmut_orig"] == [
            "    return name.strip()"
        ]

    def test_the_dispatch_table_is_not_swallowed_into_a_body(self) -> None:
        """mutmut appends `x_<fn>__mutmut_mutants = {...}` after the last variant;
        it is not code under test and must not appear in a diff."""
        body = mutants.mutant_bodies(MUTATED_SOURCE)["x_is_fresh__mutmut_2"]

        assert body == ["    if ttl <= 1:", "        return False", "    return True"]

    def test_a_file_with_no_mutants_indexes_empty(self) -> None:
        assert mutants.mutant_bodies("def plain(x):\n    return x\n") == {}


class TestBodyDiff:
    def test_reports_only_the_changed_lines(self) -> None:
        diff = mutants.body_diff(["    a = 1", "    b = 2"], ["    a = 1", "    b = 3"])

        assert diff == ("-    b = 2", "+    b = 3")

    def test_drops_the_file_header_but_keeps_removed_lines(self) -> None:
        """`--- `/`+++ ` headers start with the same characters as real diff
        lines, and a `-` line is the half that says what the code used to do."""
        diff = mutants.body_diff(["    return True"], [])

        assert diff == ("-    return True",)

    def test_identical_bodies_diff_to_nothing(self) -> None:
        assert mutants.body_diff(["    a = 1"], ["    a = 1"]) == ()


class TestSurvivors:
    @staticmethod
    def _meta(codes: dict[str, int]) -> dict:
        return {"exit_code_by_key": codes}

    def test_only_survivors_are_reported(self) -> None:
        found = mutants.survivors(
            self._meta({"m.x_is_fresh__mutmut_1": SURVIVED, "m.x_is_fresh__mutmut_2": KILLED}),
            MUTATED_SOURCE,
        )

        assert [item.key for item in found] == ["m.x_is_fresh__mutmut_1"]

    def test_no_tests_is_not_a_survivor_here(self) -> None:
        """The baseline counts "no test at all" alongside survivors, but there is
        no diff to triage for a mutant nothing ran -- the fix is a test for the
        whole function, not for that line."""
        found = mutants.survivors(self._meta({"m.x_is_fresh__mutmut_1": NO_TESTS}), MUTATED_SOURCE)

        assert found == []

    def test_the_diff_is_against_the_unmutated_original(self) -> None:
        found = mutants.survivors(self._meta({"m.x_is_fresh__mutmut_1": SURVIVED}), MUTATED_SOURCE)

        assert found[0].diff == ("-    if ttl <= 0:", "+    if ttl < 0:")
        assert found[0].function == "is_fresh"

    def test_filters_by_function_name_fragment(self) -> None:
        found = mutants.survivors(
            self._meta({"m.x_is_fresh__mutmut_1": SURVIVED, "m.x_other__mutmut_1": SURVIVED}),
            MUTATED_SOURCE,
            needle="other",
        )

        assert [item.function for item in found] == ["other"]

    def test_a_mutant_with_no_locatable_body_is_still_reported(self) -> None:
        """Silently dropping it would under-report the count this command exists
        to explain, which reads as progress."""
        found = mutants.survivors(self._meta({"m.x_gone__mutmut_1": SURVIVED}), MUTATED_SOURCE)

        assert len(found) == 1
        assert found[0].diff == ()
        assert "<no diff found>" in found[0].format()

    def test_missing_metadata_is_not_an_error(self) -> None:
        assert mutants.survivors({}, MUTATED_SOURCE) == []


class TestSurvivorsByFunction:
    def test_counts_worst_function_first(self) -> None:
        """This ordering is the triage running order, so it is load-bearing."""
        found = [
            mutants.Survivor(key="m.x_a__mutmut_1", function="a", diff=()),
            mutants.Survivor(key="m.x_b__mutmut_1", function="b", diff=()),
            mutants.Survivor(key="m.x_b__mutmut_2", function="b", diff=()),
        ]

        assert list(mutants.survivors_by_function(found)) == ["b", "a"]
        assert mutants.survivors_by_function(found) == {"b": 2, "a": 1}


class TestFindMeta:
    def test_resolves_a_bare_stem(self, tmp_path: Path) -> None:
        write_meta(tmp_path, "src/homelab/op_secrets.py", {"x": SURVIVED})

        assert mutants.find_meta(tmp_path, "op_secrets").name == "op_secrets.py.meta"

    def test_an_ambiguous_fragment_raises_rather_than_guessing(self, tmp_path: Path) -> None:
        """Inspecting the wrong file would show no survivors, which reads as done."""
        write_meta(tmp_path, "src/homelab/crap.py", {"x": SURVIVED})
        write_meta(tmp_path, "src/homelab/hosts.py", {"x": SURVIVED})

        with pytest.raises(LookupError, match="matches 2 scored files"):
            mutants.find_meta(tmp_path, ".py")

    def test_an_unknown_fragment_lists_what_was_scored(self, tmp_path: Path) -> None:
        write_meta(tmp_path, "src/homelab/crap.py", {"x": SURVIVED})

        with pytest.raises(LookupError, match="crap.py"):
            mutants.find_meta(tmp_path, "nope")

    def test_an_unswept_tree_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(LookupError, match="run a sweep first"):
            mutants.find_meta(tmp_path, "anything")


class TestReadSurvivors:
    def test_pairs_the_sidecar_with_its_mutated_source(self, tmp_path: Path) -> None:
        write_scored_file(
            tmp_path, "src/homelab/f.py", MUTATED_SOURCE, {"m.x_is_fresh__mutmut_1": SURVIVED}
        )

        found = mutants.read_survivors(tmp_path, "f.py")

        assert [item.diff for item in found] == [("-    if ttl <= 0:", "+    if ttl < 0:")]

    def test_a_file_the_sweep_never_measured_is_refused(self, tmp_path: Path) -> None:
        """A narrowed sweep still leaves a sidecar for the files it skipped, with
        no outcomes in it. Returning "no survivors" there would report an
        unjudged file as clean -- the same lie `read_results` avoids by omitting
        it from the report entirely."""
        write_scored_file(tmp_path, "src/homelab/f.py", MUTATED_SOURCE, {})

        with pytest.raises(LookupError, match="no results in this sweep"):
            mutants.read_survivors(tmp_path, "f.py")

    def test_a_file_whose_mutants_were_all_killed_is_not_refused(self, tmp_path: Path) -> None:
        """Measured and clean is a real state, and must stay distinguishable from
        never measured."""
        write_scored_file(
            tmp_path, "src/homelab/f.py", MUTATED_SOURCE, {"m.x_is_fresh__mutmut_1": KILLED}
        )

        assert mutants.read_survivors(tmp_path, "f.py") == []


class TestCliSurvivors:
    def test_prints_the_diff_and_the_per_function_tally(self, tmp_path: Path, capsys) -> None:
        write_scored_file(
            tmp_path / mutants.MUTANTS_DIRNAME,
            "src/homelab/f.py",
            MUTATED_SOURCE,
            {"m.x_is_fresh__mutmut_1": SURVIVED},
        )

        assert cli.run_survivors(tmp_path, "f.py", "", 10) == 0
        out = capsys.readouterr().out
        assert "+    if ttl < 0:" in out
        assert "by function: is_fresh 1" in out

    def test_a_clean_file_reports_success(self, tmp_path: Path, capsys) -> None:
        write_scored_file(
            tmp_path / mutants.MUTANTS_DIRNAME,
            "src/homelab/f.py",
            MUTATED_SOURCE,
            {"m.x_is_fresh__mutmut_1": KILLED},
        )

        assert cli.run_survivors(tmp_path, "f.py", "", 10) == 0
        assert "no surviving mutants" in capsys.readouterr().out

    def test_an_unresolvable_target_exits_nonzero(self, tmp_path: Path, capsys) -> None:
        """The diagnostic goes to stderr, so piping the survivor list somewhere
        does not swallow the reason it is empty."""
        (tmp_path / mutants.MUTANTS_DIRNAME).mkdir()

        assert cli.run_survivors(tmp_path, "f.py", "", 10) == 1
        captured = capsys.readouterr()
        assert "no mutation results" in captured.err
        assert captured.out == ""

    def test_top_truncates_and_says_it_did(self, tmp_path: Path, capsys) -> None:
        """A truncated list that did not admit it would look like the whole
        backlog, and the tally below it would disagree."""
        write_scored_file(
            tmp_path / mutants.MUTANTS_DIRNAME,
            "src/homelab/f.py",
            MUTATED_SOURCE,
            {"m.x_is_fresh__mutmut_1": SURVIVED, "m.x_other__mutmut_1": SURVIVED},
        )

        assert cli.run_survivors(tmp_path, "f.py", "", 1) == 1 - 1
        out = capsys.readouterr().out
        assert "1 shown; --top for more" in out
        assert "2 surviving mutant(s)" in out

    def test_the_command_is_wired_up(self, tmp_path: Path, monkeypatch) -> None:
        write_scored_file(
            tmp_path / mutants.MUTANTS_DIRNAME,
            "src/homelab/f.py",
            MUTATED_SOURCE,
            {"m.x_is_fresh__mutmut_1": SURVIVED},
        )
        monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)

        result = CliRunner().invoke(cli.main, ["survivors", "f.py", "is_fresh"])

        assert result.exit_code == 0
        assert "+    if ttl < 0:" in result.output
