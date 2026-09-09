from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from homelab import crap


def complexity_of(source: str, name: str = "f") -> int:
    tree = ast.parse(textwrap.dedent(source))
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name == name
    )
    return crap.cyclomatic_complexity(node)


class TestCyclomaticComplexity:
    def test_straight_line_function_is_one(self) -> None:
        assert complexity_of("def f():\n    return 1\n") == 1

    def test_if_adds_one_and_else_does_not(self) -> None:
        assert complexity_of("def f(x):\n    if x:\n        return 1\n    return 2\n") == 2
        assert (
            complexity_of("def f(x):\n    if x:\n        return 1\n    else:\n        return 2\n")
            == 2
        )

    def test_boolop_counts_each_extra_operand(self) -> None:
        # `a and b and c` is two decisions, not one.
        assert complexity_of("def f(a, b, c):\n    return a and b and c\n") == 3

    def test_each_except_handler_counts(self) -> None:
        source = """
        def f():
            try:
                g()
            except ValueError:
                pass
            except KeyError:
                pass
        """
        assert complexity_of(source) == 3

    def test_comprehension_counts_loop_and_filters(self) -> None:
        assert complexity_of("def f(xs):\n    return [x for x in xs if x if x > 1]\n") == 4

    def test_nested_function_is_not_folded_into_parent(self) -> None:
        source = """
        def f(x):
            def inner(y):
                if y:
                    return 1
                return 2
            if x:
                return inner
            return None
        """
        # Parent keeps only its own `if`; inner is scored as its own unit.
        assert complexity_of(source, "f") == 2
        assert complexity_of(source, "inner") == 2

    def test_match_statement_counts_cases(self) -> None:
        source = """
        def f(x):
            match x:
                case 1:
                    return "a"
                case _:
                    return "b"
        """
        assert complexity_of(source) == 3


class TestCrapScore:
    def test_full_coverage_reduces_to_complexity(self) -> None:
        assert crap.crap_score(17, 100.0) == 17

    def test_zero_coverage_is_quadratic(self) -> None:
        assert crap.crap_score(12, 0.0) == 12**2 + 12

    def test_trivial_untested_code_stays_under_threshold(self) -> None:
        # A 1-complexity getter with no tests must not be flagged.
        assert crap.crap_score(1, 0.0) < crap.DEFAULT_THRESHOLD

    def test_threshold_boundary_matches_crap4j(self) -> None:
        # The canonical pair: complexity 5 untested, complexity 30 fully covered.
        assert crap.crap_score(5, 0.0) == 30
        assert crap.crap_score(30, 100.0) == 30

    def test_coverage_lowers_score_monotonically(self) -> None:
        scores = [crap.crap_score(10, pct) for pct in (0, 25, 50, 75, 100)]
        assert scores == sorted(scores, reverse=True)


class TestFunctionsByLine:
    def test_qualifies_methods_with_class_name(self) -> None:
        source = "class C:\n    def m(self):\n        return 1\n"
        assert crap.functions_by_line(source)[2] == ("C.m", 1)

    def test_decorated_function_is_registered_at_decorator_and_def(self) -> None:
        source = "@deco\ndef f():\n    return 1\n"
        found = crap.functions_by_line(source)
        assert found[1] == ("f", 1)
        assert found[2] == ("f", 1)


class TestScoreReport:
    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        (tmp_path / "mod.py").write_text(
            "def risky(a, b):\n"
            "    if a:\n"
            "        if b:\n"
            "            return 1\n"
            "    return 0\n"
            "\n"
            "def plain():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        return tmp_path

    @staticmethod
    def report(functions: dict[str, dict]) -> dict:
        return {"files": {"mod.py": {"functions": functions}}}

    def test_scores_and_sorts_worst_first(self, root: Path) -> None:
        rows = crap.score_report(
            self.report(
                {
                    "risky": {
                        "start_line": 1,
                        "summary": {"num_statements": 4, "percent_covered": 0.0},
                    },
                    "plain": {
                        "start_line": 7,
                        "summary": {"num_statements": 1, "percent_covered": 100.0},
                    },
                }
            ),
            root,
        )
        assert [row.name for row in rows] == ["risky", "plain"]
        assert rows[0].complexity == 3
        assert rows[0].score == pytest.approx(3**2 + 3)
        assert rows[1].score == pytest.approx(1.0)

    def test_missing_start_line_is_skipped_not_guessed(self, root: Path) -> None:
        # Pre-7.13 coverage reports have no start_line; scoring must yield nothing
        # rather than mis-attributing coverage to the wrong function.
        rows = crap.score_report(
            self.report({"risky": {"summary": {"num_statements": 4, "percent_covered": 0.0}}}),
            root,
        )
        assert rows == []

    def test_statementless_function_is_skipped(self, root: Path) -> None:
        rows = crap.score_report(
            self.report(
                {"risky": {"start_line": 1, "summary": {"num_statements": 0, "percent_covered": 0}}}
            ),
            root,
        )
        assert rows == []

    def test_deleted_source_file_is_skipped(self, tmp_path: Path) -> None:
        rows = crap.score_report(
            self.report(
                {"risky": {"start_line": 1, "summary": {"num_statements": 4, "percent_covered": 0}}}
            ),
            tmp_path,
        )
        assert rows == []

    def test_over_threshold_filters_on_score(self, root: Path) -> None:
        rows = crap.score_report(
            self.report(
                {
                    "risky": {
                        "start_line": 1,
                        "summary": {"num_statements": 4, "percent_covered": 0.0},
                    },
                    "plain": {
                        "start_line": 7,
                        "summary": {"num_statements": 1, "percent_covered": 100.0},
                    },
                }
            ),
            root,
        )
        assert [row.name for row in crap.over_threshold(rows, threshold=5)] == ["risky"]
        assert crap.over_threshold(rows) == []


def row(name: str, score: float, filename: str = "mod.py") -> crap.CrapRow:
    return crap.CrapRow(
        score=score, complexity=5, coverage=0.0, filename=filename, name=name, line=1
    )


class TestBaselineKey:
    def test_key_is_file_and_name_without_the_line(self) -> None:
        # Keying on the line would make every edit above a function look like a
        # new offender, and hide real regressions in the churn.
        assert crap.baseline_key(row("f", 12.0)) == "mod.py::f"

    def test_same_name_in_two_files_is_two_entries(self) -> None:
        keys = {crap.baseline_key(row("deploy_host", 12.0, name)) for name in ("a.py", "b.py")}
        assert len(keys) == 2


class TestBaselineFile:
    def test_missing_file_grandfathers_nothing(self, tmp_path: Path) -> None:
        assert crap.load_baseline(tmp_path / "absent.json") == {}

    def test_write_records_only_functions_over_the_gate(self, tmp_path: Path) -> None:
        path = tmp_path / crap.BASELINE_FILENAME
        crap.write_baseline(path, [row("bad", 12.0), row("fine", 9.9)])
        assert crap.load_baseline(path) == {"mod.py::bad": 12.0}

    def test_round_trip_preserves_scores(self, tmp_path: Path) -> None:
        path = tmp_path / crap.BASELINE_FILENAME
        crap.write_baseline(path, [row("a", 31.116), row("b", 12.264)])
        assert crap.load_baseline(path) == {"mod.py::a": 31.1, "mod.py::b": 12.3}

    def test_rounding_stays_well_inside_the_tolerance(self, tmp_path: Path) -> None:
        # Writing a rounded score must never make the next run look regressed.
        path = tmp_path / crap.BASELINE_FILENAME
        crap.write_baseline(path, [row("a", 12.349)])
        verdict = crap.check_baseline([row("a", 12.349)], crap.load_baseline(path))
        assert not verdict.failed


class TestCheckBaseline:
    def test_new_function_over_the_gate_fails(self) -> None:
        verdict = crap.check_baseline([row("fresh", 12.0)], {})
        assert [item.name for item in verdict.new] == ["fresh"]
        assert verdict.failed

    def test_new_function_under_the_gate_passes(self) -> None:
        verdict = crap.check_baseline([row("fresh", 9.9)], {})
        assert not verdict.failed

    def test_grandfathered_function_at_its_baseline_passes(self) -> None:
        verdict = crap.check_baseline([row("old", 31.1)], {"mod.py::old": 31.1})
        assert not verdict.failed

    def test_grandfathered_function_getting_worse_fails(self) -> None:
        verdict = crap.check_baseline([row("old", 40.0)], {"mod.py::old": 31.1})
        assert verdict.regressed[0][1] == 31.1
        assert verdict.failed

    def test_coverage_noise_within_tolerance_is_not_a_regression(self) -> None:
        verdict = crap.check_baseline([row("old", 31.4)], {"mod.py::old": 31.1})
        assert not verdict.failed

    def test_improved_function_is_reported_as_cleared_not_failed(self) -> None:
        # Dropping under the gate must free the entry, or the baseline becomes a
        # permanent exemption list instead of a ratchet.
        verdict = crap.check_baseline([row("old", 4.0)], {"mod.py::old": 31.1})
        assert verdict.cleared == ["mod.py::old"]
        assert not verdict.failed

    def test_deleted_function_is_reported_as_cleared(self) -> None:
        verdict = crap.check_baseline([], {"mod.py::gone": 31.1})
        assert verdict.cleared == ["mod.py::gone"]
        assert not verdict.failed

    def test_a_stale_baseline_entry_never_exempts_a_different_function(self) -> None:
        verdict = crap.check_baseline([row("other", 12.0)], {"mod.py::old": 99.0})
        assert [item.name for item in verdict.new] == ["other"]
        assert verdict.cleared == ["mod.py::old"]
