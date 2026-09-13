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

import difflib
import hashlib
import json
import re
import shutil
from collections import Counter
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


def annotations(path: Path) -> dict[str, object]:
    """Underscore-prefixed notes already in the baseline, other than `_comment`.

    These record *how* the figures were measured -- serial-only, which artifacts
    inflated older numbers -- which is exactly the context a later reader needs and
    cannot reconstruct. Rewriting the file used to drop them, leaving a documented
    "restore it by hand afterwards" step that is trivially forgotten.
    """
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: value
        for key, value in data.items()
        if key.startswith("_") and key != "_comment"
    }


def write_baseline(path: Path, scores: list[FileScore]) -> None:
    """Rewrite the baseline from a sweep, recording only files with undetected mutants.

    Only files this sweep actually measured are rewritten; entries for files it
    did not touch are preserved, so a scoped run cannot quietly amnesty the rest.
    Hand-written `_`-prefixed notes are carried over for the same reason.
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
        **annotations(path),
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


# --- Survivor inspection -----------------------------------------------------
#
# `mutmut show` cannot resolve a mutant in this tree (it re-derives paths from
# its own config and comes up empty), and a survivor count alone does not tell
# you what to write a test for. The mutated source mutmut leaves behind is
# enough on its own: every scoped function is rewritten as a family of
# `x_<name>__mutmut_<n>` variants beside an unmutated `x_<name>__mutmut_orig`,
# so the exact change is a diff between two function bodies in one file.

MUTANT_DEF_RE = re.compile(r"^\s*def (?P<name>x_\w+__mutmut_(?:orig|\d+))\s*\(")
# What ends a mutant body: any other definition, or the trailing dispatch table
# mutmut appends (`x_foo__mutmut_mutants = {...}`).
BODY_END_RE = re.compile(r"^\s*(?:async\s+)?def\s|^\s*class\s|^x_\w+\s*=")

ORIG_SUFFIX = "__mutmut_orig"


@dataclass(frozen=True)
class Survivor:
    """One surviving mutant, reduced to the lines that actually changed."""

    key: str
    function: str
    diff: tuple[str, ...]

    def format(self) -> str:
        body = "\n".join(f"      {line}" for line in self.diff) or "      <no diff found>"
        return f"{self.key.rsplit('.', 1)[-1]}  ({self.function})\n{body}"


def function_of(mutant_name: str) -> str:
    """`x__is_cache_fresh__mutmut_7` -> `_is_cache_fresh`."""
    return re.sub(r"__mutmut_(?:orig|\d+)$", "", mutant_name).removeprefix("x_")


def mutant_bodies(source: str) -> dict[str, list[str]]:
    """Index every `x_*__mutmut_*` function body in a mutated source file."""
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for line in source.splitlines():
        match = MUTANT_DEF_RE.match(line)
        if match:
            current = match.group("name")
            bodies[current] = []
            continue
        if BODY_END_RE.match(line):
            current = None
        if current is not None and line.strip():
            bodies[current].append(line.rstrip())
    return bodies


def body_diff(original: list[str], mutated: list[str]) -> tuple[str, ...]:
    """The changed lines only.

    Zero context and no file header: the function name is already the location,
    and a mutant is a single expression, so anything more is noise to scroll
    past. Triage is a judgement about one line.
    """
    lines = difflib.unified_diff(original, mutated, n=0, lineterm="")
    return tuple(
        line for line in lines if line[:1] in {"-", "+"} and not line.startswith(("---", "+++"))
    )


def survivors(meta: dict, source: str, needle: str = "") -> list[Survivor]:
    """Surviving mutants for one scored file, optionally filtered by function name.

    A mutant whose body cannot be located still gets an entry with an empty
    diff: dropping it would silently under-report the very thing being counted.
    """
    bodies = mutant_bodies(source)
    found: list[Survivor] = []
    for key, code in (meta.get("exit_code_by_key") or {}).items():
        if code != SURVIVED_EXIT_CODE:
            continue
        name = key.rsplit(".", 1)[-1]
        function = function_of(name)
        if needle and needle not in function:
            continue
        original = bodies.get(name.split("__mutmut_")[0] + ORIG_SUFFIX, [])
        found.append(
            Survivor(key=key, function=function, diff=body_diff(original, bodies.get(name, [])))
        )
    return found


def survivors_by_function(found: Iterable[Survivor]) -> dict[str, int]:
    """Survivor counts per function, worst first — the triage running order."""
    counts = Counter(item.function for item in found)
    return dict(counts.most_common())


def find_meta(mutants_dir: Path, target: str) -> Path:
    """Resolve a file argument to one `.py.meta`, accepting any unique substring.

    `op_secrets`, `op_secrets.py` and the full path all work; an ambiguous or
    unknown fragment raises rather than guessing, since silently inspecting the
    wrong file would read as "no survivors left".
    """
    metas = sorted(mutants_dir.rglob(f"*{META_SUFFIX}"))
    if not metas:
        raise LookupError(f"no mutation results under {mutants_dir}/; run a sweep first")
    matches = [path for path in metas if target in str(path.relative_to(mutants_dir))]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(str(path.relative_to(mutants_dir))[: -len(".meta")] for path in metas)
    if not matches:
        raise LookupError(f"no scored file matches {target!r}; scored: {known}")
    raise LookupError(f"{target!r} matches {len(matches)} scored files; scored: {known}")


def read_survivors(mutants_dir: Path, target: str, needle: str = "") -> list[Survivor]:
    """Load and diff the surviving mutants of one scored file.

    A file left unmeasured by a narrowed sweep still has a `.meta` sidecar, but
    with no outcomes in it. Refusing is the same call `read_results` makes by
    omitting such a file: an empty survivor list would otherwise read as "clean"
    for a file that was never actually judged.
    """
    meta_path = find_meta(mutants_dir, target)
    filename = str(meta_path.relative_to(mutants_dir))[: -len(".meta")]
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not score_meta(filename, meta).total:
        raise LookupError(f"{filename} has no results in this sweep; sweep it before triaging")
    source = meta_path.with_suffix("").read_text(encoding="utf-8")
    return survivors(meta, source, needle)
