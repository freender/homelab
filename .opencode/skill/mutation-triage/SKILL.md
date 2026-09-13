---
name: mutation-triage
description: Spend down the mutation-testing backlog in this repo — run a sweep with `homelab mutants`, read survivors with `homelab survivors`, judge which are real gaps, write the tests that kill them, and ratchet mutation-baseline.json. Use when asked to improve mutation score, kill survivors, work the mutation backlog, or when a sweep disagrees with the baseline. For coverage and the CRAP gate, or for module/deploy work, use deploy-module and AGENTS.md instead.
---

## What this is for

A survivor is a behaviour **nothing asserts**: mutmut changed the meaning of one
expression and the suite still passed. Unlike coverage, it cannot be satisfied by
executing a line. The job is always the same shape — sweep, read the diffs, decide which
survivors are real, write asserting tests, prove the improvement twice, ratchet.

`AGENTS.md` owns the *policy* (what the gate is, why it is not in `./validate`, the
ratchet rules). This skill owns the *loop*.

## The loop

```bash
.venv/bin/python -m pip install '.[mutation]'        # one-off; deliberately not in dev
PYTHONPATH=src .venv/bin/python -m homelab.cli mutants 'homelab.op_secrets.*'   # sweep one file
PYTHONPATH=src .venv/bin/python -m homelab.cli survivors op_secrets             # what changed
PYTHONPATH=src .venv/bin/python -m homelab.cli survivors op_secrets cleanup     # one function
```

`PYTHONPATH=src` with `-m homelab.cli` is **load-bearing**; the bare `homelab` console
script resolves the repo to `.venv/lib/...` and reports "nothing scored". `survivors`
reads the existing `mutants/` tree and never sweeps, so it is instant and safe to rerun.

1. **Sweep one file at a time.** A narrowed sweep is minutes where a full one is hours.
2. **Read survivors by function, worst cluster first.** `survivors` prints the per-function
   tally last. Survivors bunch by *cause*, so one test shape usually clears several.
3. **Judge each cluster** against the two lists below.
4. **Write asserting tests.** A test that merely executes the line changes nothing here —
   that is the hole this metric exists to find.
5. **Sweep twice** (`rm -rf mutants` between) and confirm the same number before ratcheting.
   `--no-run` re-scores the *same* tree and is not an independent sample.
6. `--update-baseline`, then `./validate`, then ship.

## Rails

- **Serial only.** `--max-children` defaults to 1 and `--update-baseline` refuses anything
  else. Children share one `mutants/` tree, so a mutant that writes under it fails a
  *different* child's test and is recorded as killed. Parallel figures are biased **low** —
  use `--max-children 8` to explore, never to judge.
- **Never hand-edit `mutation-baseline.json`.** It may only shrink. Regenerate with
  `--update-baseline` to lock in an improvement, never to admit a regression.
- **Do not lower `timeout_multiplier = 60.0`** in `[tool.mutmut]` to save time. mutmut
  scores a CPU-cap timeout as *killed*, so a low multiplier silently inflates the score.
- **Measure a file twice before ratcheting.** The error is only safe in one direction: an
  entry that is too high reports as "improved", one that is too low fails the gate for
  everyone afterwards.

## Judging a survivor

Kill it when the mutant describes a **behaviour a caller could depend on**: a boundary, a
branch, an argument to a call with a side effect, a security-relevant mode, an error's
locator (key, index, line number).

Decline it when the mutant is genuinely equivalent, and say so in the commit rather than
contorting a test:

- **Error-message prose** — case flips, `XX`-wrapping of a literal. Asserting exact copy
  pins wording to no benefit. But `raise ValueError(None)` surviving is *not* prose: it
  means that raise path has no `match=` at all, and the message *locator* (a config key, a
  list index, a line number) is worth asserting as a substring.
- `strip(None)`/`rstrip` charset variants the input grammar cannot produce.
- `rsplit` maxsplit variants that cannot change a `[-1]`.
- `""` vs `None` defaults where both are falsy and the reader only tests truthiness.
- `encoding="UTF-8"` vs `"utf-8"`.
- `rmtree(ignore_errors=...)` already wrapped in `except OSError` — assert the kwarg with a
  stub instead of trying to observe it.

## Gap shapes that recur here

Found repeatedly across `normalize.py` and `op_secrets.py`. Check these first:

- **A test double that ignores its own arguments.** `lambda _name: "/usr/bin/shred"`
  answers *every* name, so it cannot tell `which("shred")` from `which("SHRED")`. This has
  been the single most productive shape — an argument-ignoring stub makes every mutation
  of the *call* invisible. Grep doubles for `lambda _`.
- **A double swallowing `**kwargs`**, leaving `check=`/`stdout=`/`stderr=` unasserted.
  `check=False` is often load-bearing: it stops a `CalledProcessError` — which is *not* an
  `OSError` — escaping a handler that only catches the latter.
- **`x.get("key", "")` feeding a string check.** Drop the default and `None` stringifies to
  `"None"`, passing every truthiness test downstream.
- **`pytest.raises(X)` with no `match=`.** Any message mutant survives.
- **Boundaries** — `<` vs `<=`, and the off-by-one literal beside it.
- **`continue` → `break` in a loop only ever tested with one element.** Common in any
  "check everything and report" routine, where stopping early defeats the purpose.
- **A payload appended to a list only used for `len()`.** Nothing reads it, so every
  mutation of it survives. The fix is usually to delete the payload, not to assert it.
- **A property only observable mid-call**, e.g. a file pre-created `0o600` before a
  subprocess writes to it and chmodded again after. The end state is identical either way;
  the stub has to observe the file *while it is notionally running*.

## Traps

- **`HOMELAB_OFFLINE=1` is set for the sweep** (`mutmut_env()` in `src/homelab/cli.py`), so
  a test needing an online path must `monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)`.
  Symptom: passes standalone, fails **only** under mutmut. Suspect the environment, not the
  copied tree.
- **mutmut caches verdicts and does not invalidate on test-only edits** — which is exactly
  what this loop consists of. `homelab mutants` fingerprints `tests/**/*.py` and discards
  the tree when it moves; narrowing with TARGETS only warns, since wiping would drop the
  untargeted files from the report.
- **`only_mutate` globs whole files — there is no function-level granularity.** The unit of
  scope is the file, so adding one means accepting all of it. This is why the leak check
  lives in `leakcheck.py` rather than in `cli.py`.
- `mutmut show` cannot resolve a mutant in this tree; that is what `homelab survivors` is
  for.

| File | When |
|---|---|
| `reference/measurement-history.md` | A sweep disagrees with the baseline, a figure looks wrong, or you are about to re-investigate why numbers drift |
