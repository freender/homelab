"""Mutation scoring over mutmut's per-file result metadata.

Mutation testing answers the question coverage cannot: if the meaning of a line
changes, does a test fail? mutmut rewrites one expression at a time — `continue`
to `break`, `>` to `>=`, a literal to a different literal — reruns the tests that
touch it, and records the pytest exit code. A mutant the suite still passes
*survived*, and a survivor is a statement of behaviour nothing asserts.

This is the complement to `crap.py`, not a replacement. CRAP reads coverage, and
coverage records execution rather than assertion, so a function can be fully
covered and still have no test that would notice it being wrong; `crap.py`'s own
docstring says as much. Mutation score is the check that closes that gap, at
roughly a thousand times the cost — hence `homelab mutants` out of band rather
than a `./validate` step.

The ratchet here mirrors `crap-baseline.json`: per-file undetected-mutant counts
that may only shrink.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# mutmut's working directory: a full copy of the repo with one source file
# mutated per child run, plus a `<file>.py.meta` sidecar holding the outcomes.
MUTANTS_DIRNAME = "mutants"
META_SUFFIX = ".py.meta"

BASELINE_FILENAME = "mutation-baseline.json"

# Our own sidecar inside `mutants/`, not one of mutmut's. See `suite_changed`.
SUITE_FINGERPRINT_FILENAME = "homelab-test-fingerprint"

# Outcome buckets, keyed by the pytest exit code mutmut stored. Mirrors mutmut's
# own `status_by_exit_code`; tests/test_mutants.py asserts the two agree whenever
# mutmut is importable, so this table cannot drift silently. It already caught
# one upstream change (3.8 added -9 and 37), which is the point of the guard.
#
# Timeouts and segfaults count as detected: the mutant made the suite hang or
# die, which is a failure the suite did surface.
DETECTED_EXIT_CODES = frozenset({1, 3, -9, -11, -24, 24, 36, 37, 152, 255})
SURVIVED_EXIT_CODE = 0
# "no tests": mutmut found no test exercising the mutated function at all.
NO_TEST_EXIT_CODES = frozenset({5, 33})

# Which files are mutated is owned by `[tool.mutmut].only_mutate` in
# pyproject.toml, not repeated here: unscoped files are copied into the working
# tree unmutated and leave no metadata behind, so they simply never appear in a
# report. `homelab mutants` therefore has no default target list of its own.


def suite_fingerprint(paths: Iterable[Path]) -> str:
    """A content hash of the test suite, stable across runs and path ordering.

    Unreadable or vanished files hash as absent rather than raising: a fingerprint
    is a change detector, and the worst a wrong answer costs is one extra sweep.
    """
    digest = hashlib.sha256()
    for path in sorted({Path(p) for p in paths}, key=str):
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        except OSError:
            digest.update(b"missing")
        digest.update(b"\n")
    return digest.hexdigest()


def read_suite_fingerprint(mutants_dir: Path) -> str | None:
    """The fingerprint recorded by the last completed sweep, if there was one."""
    path = mutants_dir / SUITE_FINGERPRINT_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_suite_fingerprint(mutants_dir: Path, fingerprint: str) -> None:
    mutants_dir.mkdir(parents=True, exist_ok=True)
    (mutants_dir / SUITE_FINGERPRINT_FILENAME).write_text(fingerprint + "\n", encoding="utf-8")


def suite_changed(mutants_dir: Path, fingerprint: str) -> bool:
    """Whether the tests changed since the results in `mutants_dir` were produced.

    This exists because mutmut's cache is keyed on the hash of the *mutated source*
    (`current_function_hashes`, built only from `walk_source_files()`), plus its
    config fingerprint and tracked **non**-Python files. A change to a test file
    therefore invalidates nothing: mutmut reuses every cached verdict and reprints
    the previous sweep's numbers after what looks like a full run.

    That is precisely backwards for the workflow this command exists to serve —
    "add the missing assertion, re-sweep, ratchet the baseline down" is a
    test-only change every time. Without this check a session can write a hundred
    assertions, re-run the sweep, and be told nothing improved.

    No recorded fingerprint means the first sweep, or a tree written before this
    check existed; neither is evidence of staleness, so both return False.
    """
    recorded = read_suite_fingerprint(mutants_dir)
    return recorded is not None and recorded != fingerprint


def discard_results(mutants_dir: Path) -> None:
    """Drop the whole working tree so the next sweep rebuilds from scratch.

    Deleting rather than editing mutmut's `.meta` sidecars in place: resetting
    individual verdicts to `null` would work, but it is a second, undocumented
    dependency on a private file format that the reader below only has to *read*.
    """
    shutil.rmtree(mutants_dir, ignore_errors=True)


@dataclass(frozen=True)
class FileScore:
    """Mutation outcomes for one source file."""

    filename: str
    killed: int
    survived: int
    no_tests: int

    @property
    def undetected(self) -> int:
        """Mutants the suite did not catch.

        `no_tests` is counted here with `survived` deliberately: a mutant no test
        exercises is one no test would have failed on. Splitting the two apart is
        for the report; for the gate they are the same answer.
        """
        return self.survived + self.no_tests

    @property
    def total(self) -> int:
        return self.killed + self.undetected

    @property
    def score(self) -> float:
        """Percentage of mutants detected; a file with nothing run scores 100."""
        if not self.total:
            return 100.0
        return 100.0 * self.killed / self.total

    def format(self) -> str:
        return (
            f"{self.score:6.1f}%  {self.killed:5d} killed  {self.survived:5d} survived  "
            f"{self.no_tests:5d} untested  {self.filename}"
        )


def classify(exit_codes: list[int | None]) -> tuple[int, int, int]:
    """Bucket raw exit codes into (killed, survived, no_tests).

    Anything else — not yet run, skipped, interrupted — is dropped rather than
    guessed at, so a partial sweep reports only what it actually measured.
    """
    killed = survived = no_tests = 0
    for code in exit_codes:
        if code in DETECTED_EXIT_CODES:
            killed += 1
        elif code == SURVIVED_EXIT_CODE:
            survived += 1
        elif code in NO_TEST_EXIT_CODES:
            no_tests += 1
    return killed, survived, no_tests


def score_meta(filename: str, meta: dict) -> FileScore:
    """Score one `<file>.py.meta` payload."""
    codes = list((meta.get("exit_code_by_key") or {}).values())
    killed, survived, no_tests = classify(codes)
    return FileScore(filename=filename, killed=killed, survived=survived, no_tests=no_tests)


def read_results(mutants_dir: Path) -> list[FileScore]:
    """Read every scored file from a mutmut working tree, worst score first.

    Files with no mutant run in this sweep are omitted entirely — reporting an
    unmeasured file as a perfect 100% would be a lie of omission.
    """
    scores: list[FileScore] = []
    for path in sorted(mutants_dir.rglob(f"*{META_SUFFIX}")):
        filename = str(path.relative_to(mutants_dir))[: -len(".meta")]
        score = score_meta(filename, json.loads(path.read_text(encoding="utf-8")))
        if score.total:
            scores.append(score)
    scores.sort(key=lambda score: (score.score, score.filename))
    return scores


@dataclass(frozen=True)
class BaselineVerdict:
    """What the ratchet found: two failure buckets and one cleanup bucket."""

    new: list[FileScore]
    regressed: list[tuple[FileScore, int]]
    cleared: list[tuple[str, int, int]]

    @property
    def failed(self) -> bool:
        return bool(self.new or self.regressed)


def load_baseline(path: Path) -> dict[str, int]:
    """Read the grandfathered undetected-mutant counts; missing file exempts nothing."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): int(value) for key, value in (data.get("files") or {}).items()}


def write_baseline(path: Path, scores: list[FileScore]) -> None:
    """Rewrite the baseline from a sweep, recording only files with undetected mutants.

    Only files this sweep actually measured are rewritten; entries for files it
    did not touch are preserved, so a scoped run cannot quietly amnesty the rest.
    """
    files = {key: value for key, value in load_baseline(path).items()}
    for score in scores:
        files.pop(score.filename, None)
        if score.undetected:
            files[score.filename] = score.undetected
    payload = {
        "_comment": (
            "Ratchet for `homelab mutants`: undetected (survived + untested) mutants "
            "per file. Counts may be lowered or removed, never added or raised by hand. "
            "Regenerate with `homelab mutants --update-baseline` after an improvement. "
            "Scope lives in [tool.mutmut].only_mutate in pyproject.toml."
        ),
        "files": dict(sorted(files.items())),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def check_baseline(scores: list[FileScore], baseline: dict[str, int]) -> BaselineVerdict:
    """Compare a sweep against the ratchet.

    A file with undetected mutants that the baseline does not own fails, and a
    file that got worse fails. A file that improved is reported as `cleared` so
    the entry can be lowered — that is what makes the baseline shrink rather than
    become a permanent exemption list.
    """
    new: list[FileScore] = []
    regressed: list[tuple[FileScore, int]] = []
    cleared: list[tuple[str, int, int]] = []

    for score in scores:
        recorded = baseline.get(score.filename)
        if recorded is None:
            if score.undetected:
                new.append(score)
        elif score.undetected > recorded:
            regressed.append((score, recorded))
        elif score.undetected < recorded:
            cleared.append((score.filename, recorded, score.undetected))

    return BaselineVerdict(new=new, regressed=regressed, cleared=cleared)


def summarize(scores: list[FileScore]) -> FileScore:
    """Roll a sweep up into one pseudo-row for the report's bottom line."""
    return FileScore(
        filename=f"{len(scores)} file(s)",
        killed=sum(score.killed for score in scores),
        survived=sum(score.survived for score in scores),
        no_tests=sum(score.no_tests for score in scores),
    )
