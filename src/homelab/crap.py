"""CRAP (Change Risk Anti-Patterns) scoring over a coverage.py JSON report.

    CRAP(m) = comp(m)^2 * (1 - cov(m)/100)^3 + comp(m)

Cyclomatic complexity comes from the AST; per-function coverage comes from
coverage.py's JSON report, which carries a `start_line` per function that we join
on. The score is deliberately report-only: coverage measures execution, not
assertion, so a high score flags code worth *looking* at and nothing more. See
`validate`'s "Code Risk" step for how it is surfaced.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# The conventional Crap4J threshold: complexity 5 untested, or 30 fully tested.
DEFAULT_THRESHOLD = 30.0

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
