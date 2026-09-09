"""CRAP (Change Risk Anti-Patterns) scoring over a coverage.py JSON report.

    CRAP(m) = comp(m)^2 * (1 - cov(m)/100)^3 + comp(m)

Cyclomatic complexity comes from the AST; per-function coverage comes from
coverage.py's JSON report, which carries a `start_line` per function that we join
on. `validate` gates on the score via a shrinking baseline; see its "Code Risk"
step and `check_baseline` below.

Coverage records execution, not assertion, so the score is gameable by tests that
run code without checking it — `tests/test_dry_run_all_modules.py` asserts only
`exit_code == 0`. That is why the gate sits well above the "every function is
tested" line: at 100% coverage CRAP degenerates to plain complexity, so
FAIL_THRESHOLD is first a complexity ceiling and only second a coverage one.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

# The conventional Crap4J threshold: complexity 5 untested, or 30 fully tested.
# Reporting-only; nothing fails on it.
DEFAULT_THRESHOLD = 30.0

# What `validate` actually fails on. At full coverage CRAP == complexity, so this
# reads as "no function above complexity 10, and anything simpler must be tested
# in proportion to how branchy it is" (complexity 3 untested scores 12 and fails;
# complexity 3 at 50% coverage scores 4.1 and passes).
FAIL_THRESHOLD = 10.0

# Where the ratchet's grandfathered scores live, relative to the repo root.
BASELINE_FILENAME = "crap-baseline.json"

# A function's coverage moves slightly when unrelated tests change what they
# happen to execute, so the ratchet only trips on a rise it cannot call noise.
BASELINE_TOLERANCE = 0.5

_FUNC_NODES = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class CrapRow:
    """One scored function."""

    score: float
    complexity: int
    coverage: float
    filename: str
    name: str
    line: int

    def format(self) -> str:
        return (
            f"{self.score:7.1f}  cx {self.complexity:>3}  {self.coverage:5.1f}%  "
            f"{self.filename}:{self.line} {self.name}()"
        )


def crap_score(complexity: int, coverage: float) -> float:
    """CRAP for one function; `coverage` is a percentage in 0..100."""
    uncovered = 1 - coverage / 100
    return complexity**2 * uncovered**3 + complexity


def cyclomatic_complexity(node: ast.AST) -> int:
    """McCabe complexity of a single function, excluding nested definitions.

    Nested functions and classes are separate units with their own coverage
    entries, so folding them into the parent would double-count them.
    """
    score = 1
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, _FUNC_NODES | ast.ClassDef):
            continue
        if isinstance(child, ast.If | ast.IfExp | ast.For | ast.AsyncFor | ast.While):
            score += 1
        elif isinstance(child, ast.ExceptHandler | ast.Assert | ast.match_case):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
        stack.extend(ast.iter_child_nodes(child))
    return score


def functions_by_line(source: str, filename: str = "<source>") -> dict[int, tuple[str, int]]:
    """Map each plausible start line to (qualified name, complexity).

    coverage.py anchors a function region at its `def`, but decorated functions can
    be reported from the first decorator, so every candidate line is registered.
    """
    tree = ast.parse(source, filename=filename)
    found: dict[int, tuple[str, int]] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, _FUNC_NODES):
                name = f"{prefix}{child.name}"
                entry = (name, cyclomatic_complexity(child))
                for line in {child.lineno, *(dec.lineno for dec in child.decorator_list)}:
                    found[line] = entry
                walk(child, f"{name}.")

    walk(tree, "")
    return found


def score_report(report: dict, root: Path) -> list[CrapRow]:
    """Score every function in a parsed coverage JSON report, worst first.

    Files that no longer exist and functions with no statements are skipped rather
    than guessed at; a report predating coverage 7.13's `start_line` yields nothing,
    which callers surface as a skip.
    """
    rows: list[CrapRow] = []
    for filename, file_report in report.get("files", {}).items():
        path = root / filename
        if not path.is_file():
            continue
        lines = functions_by_line(path.read_text(encoding="utf-8"), filename)
        for key, entry in file_report.get("functions", {}).items():
            start = entry.get("start_line")
            summary = entry.get("summary", {})
            if start is None or not summary.get("num_statements"):
                continue
            # Tolerate the region being anchored one line off the `def` itself.
            match = lines.get(start) or lines.get(start + 1)
            if match is None:
                continue
            name, complexity = match
            coverage = float(summary["percent_covered"])
            rows.append(
                CrapRow(
                    score=crap_score(complexity, coverage),
                    complexity=complexity,
                    coverage=coverage,
                    filename=filename,
                    name=key or name,
                    line=start,
                )
            )
    rows.sort(key=lambda row: (-row.score, row.filename, row.line))
    return rows


def over_threshold(rows: list[CrapRow], threshold: float = DEFAULT_THRESHOLD) -> list[CrapRow]:
    return [row for row in rows if row.score > threshold]


@dataclass(frozen=True)
class BaselineVerdict:
    """What the ratchet found: two failure buckets and one cleanup bucket."""

    new: list[CrapRow]
    regressed: list[tuple[CrapRow, float]]
    cleared: list[str]

    @property
    def failed(self) -> bool:
        return bool(self.new or self.regressed)


def baseline_key(row: CrapRow) -> str:
    """Identify a function by file and qualified name, never by line.

    Line numbers move on every edit above them; keying on one would make the
    baseline fail on unrelated changes and hide a real regression behind the
    resulting churn.
    """
    return f"{row.filename}::{row.name}"


def load_baseline(path: Path) -> dict[str, float]:
    """Read the grandfathered scores; a missing file means nothing is exempt."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): float(value) for key, value in (data.get("functions") or {}).items()}


def write_baseline(path: Path, rows: list[CrapRow], threshold: float = FAIL_THRESHOLD) -> None:
    """Rewrite the baseline from a scored run, recording only what is over the gate."""
    functions = {
        baseline_key(row): round(row.score, 1) for row in over_threshold(rows, threshold)
    }
    payload = {
        "_comment": (
            "Ratchet for validate's CRAP gate: functions grandfathered above "
            f"{threshold:g}. Entries may be removed or lowered, never added or raised "
            "by hand. Regenerate with `homelab crap --update-baseline`."
        ),
        "threshold": threshold,
        "functions": dict(sorted(functions.items())),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def check_baseline(
    rows: list[CrapRow],
    baseline: dict[str, float],
    threshold: float = FAIL_THRESHOLD,
    tolerance: float = BASELINE_TOLERANCE,
) -> BaselineVerdict:
    """Compare a scored run against the ratchet.

    New code over the gate fails, and a grandfathered function that got worse
    fails. A grandfathered function that dropped under the gate (or was deleted)
    is reported as `cleared` so the entry can be dropped: that is what makes the
    baseline shrink rather than become a permanent exemption list.
    """
    new: list[CrapRow] = []
    regressed: list[tuple[CrapRow, float]] = []
    seen: set[str] = set()

    for row in over_threshold(rows, threshold):
        key = baseline_key(row)
        seen.add(key)
        recorded = baseline.get(key)
        if recorded is None:
            new.append(row)
        elif row.score > recorded + tolerance:
            regressed.append((row, recorded))

    return BaselineVerdict(new=new, regressed=regressed, cleared=sorted(set(baseline) - seen))
