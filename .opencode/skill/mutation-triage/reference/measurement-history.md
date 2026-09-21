# Measurement history — why the figures are what they are

Read this when a sweep disagrees with `mutation-baseline.json`, when a number looks wrong,
or before re-opening the question of why mutation figures drift. Two artifacts have already
been found and fixed; five hypotheses are dead. Do not re-propose them.

## Artifact 1 — timeouts scored as kills (fixed in `1839313`)

mutmut puts a CPU-seconds cap on each mutant, `(estimated_test_time + timeout_constant) *
timeout_multiplier * 2`, and **scores a mutant that hits it as killed** (SIGXCPU, exit
`-24`). `DETECTED_EXIT_CODES` mirrors mutmut here deliberately, on the theory that a hang
is a detection.

At the stock multiplier that cap fired on hundreds of mutants that were not hanging at all,
so they were recorded as caught without ever being judged. One such sweep scored `hosts.py`
at **16** undetected against its true 38. The tell was in the exit codes: `crap.py` and
`leakcheck.py` took zero timeouts and were the only files that never moved, while
`op_secrets.py` took 112.

`timeout_multiplier = 60.0` in `[tool.mutmut]` fixes it — the whole sweep then records zero
timeouts and every verdict is a real test outcome.

## Artifact 2 — parallel children faking kills (fixed in `4f1048b`)

Every child shares the *same* `mutants/` working tree, so a mutant that writes under it — a
staging helper redirected into a repo `build/` dir, a secret written somewhere other than
tmpfs — makes a **different** child's test fail, and that unrelated mutant is recorded as
killed. Parallel sweeps are therefore biased *low*, and not by a little: `normalize.py`
read 163–165 across parallel sweeps against a true 184 at the time.

**The tell is which files hold still.** `crap.py` 46, `hosts.py` 38 and `leakcheck.py` 44
score identically parallel or serial, because their tests only read. The three that
moved — `module_support.py` 31–37, `normalize.py` 163–165, `op_secrets.py` 124–126 — are
exactly the three whose code writes.

**Any figure older than `1839313` predates artifact 1; anything older than `4f1048b`
carries artifact 2 as well. Do not compare against either.**

## Five dead hypotheses

Before the two artifacts above were found, the drift was blamed on each of these in turn.
All are disproved:

1. **I/O in the naive sense** — `leakcheck.py` shells out to `git ls-files` and is exact.
2. **`tests/test_dry_run_all_modules.py` breadth** — the correlation held only while
   `op_secrets.py` looked stable, which was artifact 1.
3. **File size** — no relationship.
4. **Hash randomisation or test ordering** — `PYTHONHASHSEED=0` still gave 37/35/33, and
   neither `pytest-randomly` nor `xdist` is installed.
5. **mutmut's stats phase** — two fresh collections produce byte-identical test-selection
   maps.

## Not a nightly CI job

The scheduled workflow built and reverted on 2026-09-12 stays reverted. The reason is now
duller than irreproducibility: an honest serial sweep of all six files takes hours, against
the under-a-minute `./validate` it would sit beside. Whether serial figures hold across
*machines* is untested — the old CI numbers (36–37, 176–179) came from parallel runs with
timeouts and prove nothing either way.
