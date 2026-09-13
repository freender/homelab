# Homelab Agent Guide (AGENTS.md)

Python-orchestrated automation for a Proxmox homelab. Python + Bash + YAML; inventory in
`hosts.conf`; local orchestration in `src/homelab/`; shared remote libs in `lib/utils.sh`
and `lib/print.sh`. Pattern: a Python module builds and stages files, then runs a remote
`scripts/install.sh`.

- **Repo work** (edit `src/homelab/`, add/modify a module, `./validate`, or `./deploy`
  dry-run or live) -> the commands below plus the `deploy-module` skill. Topology isn't
  needed; `./deploy` reads `hosts.conf` itself.
- **Live operation** (SSH to inspect/debug a host, cross-host investigation not covered by
  `./deploy`) -> the `homelab-infra` skill for topology, then the relevant topic skill.

## Public Repo Boundary

**`freender/homelab` is a public GitHub repo.** Everything committed is world-readable and
permanent, including history. Treat every edit as publication.

**Never commit:**
- The real public domain used for homelab service routes, or any externally reachable
  route host/URL — not in docs, comments, tests, or CI config. It appears zero times
  today; keep it that way. Use `example.net` placeholders, as templates already do
  (`traefik-tower.example.net`).
- Secrets in any form: `.env`, rendered secret files, tokens, API keys, passwords, SSH
  private keys, PBS encryption keys **or their escrow locations**.
- Third-party detail for `cinci`/`cottonwood` beyond what inventory needs — physical
  location, LAN topology, the owners' personal data. These are other people's networks.
- Live security posture: WireGuard/Pi-KVM route topology, Crowdsec/middleware allowlists,
  per-host SSH auth matrices, agent socket paths, credential file locations.

**Intentionally public — do NOT "sanitize" these:** `*.freender.internal` hostnames,
RFC1918 IPs, usernames, NIC MACs, SSH *public* keys, PBS account/token *names*, and
schedules. `hosts.conf` depends on them; scrubbing them breaks deploys. `.internal` is
split-horizon DNS resolvable only on the LAN, and RFC1918 is unroutable — both are deploy
metadata, not attack surface. Do not confuse `.internal` with the public route domain
above, which is the one that must never appear here. The leak check ignores `.internal`,
`.local`, `.lan`, `.invalid`, and `.test` by design, and allows vendor URLs
(`github.com`, `download.proxmox.com`, `get.docker.com`, `api.telegram.org`).

**Skills:** only repo-scoped tooling docs belong here (`.opencode/skill/deploy-module/`).
Topology, storage, backup, SSH, offsite, monitoring, and secret-handling skills stay
host-local in `~/.config/opencode/skills/` — they are credential and recon maps, not repo
documentation.

`./validate` enforces the domain/secret half of this mechanically (`leak check`); the
judgment calls above are still yours.

## Finding Things — And Not Crawling For Them

- Host facts, SSH metadata, deploy targets, feature config -> `hosts.conf` first.
- Topology, VLANs, storage layout, heavy-path warnings, cross-host context -> the
  `homelab-infra` skill (self-contained topology map).
- Module changes -> read the orchestrator in `src/homelab/modules/` and the matching
  `<module>/scripts/install.sh` before editing.
- Docker app placement and compose definitions -> the repo copy under `docker/`; don't
  inspect live `/mnt/cache/appdata` unless a task requires a known app path.
- **Never** run broad recursive scans on a homelab host under `/`, `/mnt/*`,
  `/mnt/cache`, `/mnt/tank`, `/vm-flash`, `/backup`, or `/srv/timemachine`. `find .` is
  repo-root only — never adapt it to remote storage, media, backup, or appdata paths.
- Unknown path -> ask, or check the Homelab Obsidian docs. Don't discover by crawling.

## Build, Lint, and Test

```bash
./validate                                            # everything CI gates on — best pre-PR check
./deploy --dry-run apcupsd ace                        # single module dry-run (primary "single test")
./deploy --dry-run all all                            # full dry-run
.venv/bin/python -m pytest tests/                     # unit tests
.venv/bin/python -m ruff check src/homelab/cli.py     # targeted lint
PYTHONPATH=src .venv/bin/python -m homelab.cli crap   # CRAP scores from the last pytest run
PYTHONPATH=src .venv/bin/python -m homelab.cli mutants   # mutation sweep (slow; out of band)
shellcheck -S warning pve-postinstall/scripts/install.sh
find . -name '*.sh' -not -path './.bin/*' -exec shellcheck -S warning {} +   # repo root only
yq eval '.' hosts.conf >/dev/null
```

`./validate` runs Python compile, Ruff, Pytest, the CRAP gate, `hosts.conf` parse
validation, the inventory/module cross-check, the leak check, ShellCheck, and module
dry-runs — the same set CI runs on push/PR to `main` (`.github/workflows/validate.yml`).
Ruff and Pytest are skipped with a warning when missing,
so run it from the repo `.venv` (or `uv run`) for true CI parity. Run the targeted checks
above first and `./validate` last — it is the slowest and repeats them all. After any push,
check that push's Actions run and inspect failures immediately if any job is red.

The `deploy-module` skill carries the test coverage map (golden renders, pause semantics,
network-critical modules) and which test owns which area; update tests when touching them.

### The CRAP Gate

`CRAP = complexity^2 * (1 - coverage/100)^3 + complexity`, scored per function from the
coverage data Pytest just wrote. **`validate` fails above 10.** Because CRAP collapses to
plain complexity at 100% coverage, the gate reads first as "no function above complexity
10", and only second as a coverage rule — you cannot pass it by having
`test_dry_run_all_modules.py` merely execute the code.

`crap-baseline.json` is a **ratchet, not an exemption list**: it grandfathers the
functions that were already over 10, and it may only shrink.

- New or moved code is held to 10 from its first commit — it is never in the baseline.
- A baselined function that gets *worse* fails too (0.5 tolerance for coverage noise).
- Never hand-add or hand-raise an entry. Regenerate with
  `homelab crap --update-baseline` only to lock in an improvement.
- Clearing an entry means splitting the function or adding tests that **assert**, not
  tests that merely execute it. Coverage-gaming is this metric's known hole.

### Mutation Testing (`homelab mutants`)

The check for the hole the CRAP gate names above. mutmut rewrites one expression at a
time — `continue` to `break`, `>` to `>=` — and reruns the tests that touch it. A mutant
the suite still passes is a behaviour **nothing asserts**, which coverage cannot see.

```bash
.venv/bin/python -m pip install '.[mutation]'   # separate extra, deliberately not in dev
M=".venv/bin/python -m homelab.cli mutants"     # see the PYTHONPATH note below
PYTHONPATH=src $M                               # sweep the scoped core, then gate
PYTHONPATH=src $M --no-run                      # re-score the last sweep without redoing it
PYTHONPATH=src $M 'homelab.hosts.*'             # narrow further than the configured scope
mutmut show <mutant-name>                       # the exact surviving diff
```

**`PYTHONPATH=src` and `-m homelab.cli` are both load-bearing; the bare `homelab`
console script does not work here.** `repo_root()` is `Path(__file__).parents[2]`, and
the repo `.venv` is a *non-editable* install, so the installed script resolves the "repo"
to `.venv/lib/python3.13` — `--no-run` then reports "nothing scored" and a full sweep
would run mutmut with that as its cwd. Same workaround the dry-run job in
`validate.yml` already uses. Setting it for the parent is safe precisely because
`mutmut_env()` pops `PYTHONPATH` back off for the mutmut children, which must not see the
real `src/`.

**Not a `./validate` step and not a PR gate:** a sweep is tens of minutes against a suite
`./validate` clears in under one, so gating on it would make the fast check something you
route around. It is also a different question — `./validate` gates a *change*, this
ratchets the *suite*. Run it by hand when you change a scoped file, then fix or
re-baseline. `mutants/` is a gitignored working copy of the repo; results accumulate
there across runs.

**`timeout_multiplier = 60.0` in `[tool.mutmut]` is load-bearing — do not drop it to save
time.** mutmut puts a CPU-seconds cap on each mutant, `(estimated_test_time +
timeout_constant) * timeout_multiplier * 2`, and **scores a mutant that hits it as
killed** (SIGXCPU, exit `-24`; `DETECTED_EXIT_CODES` mirrors mutmut here deliberately, on
the theory that a hang is a detection). At the stock multiplier that cap fired on hundreds
of mutants that were not hanging at all, so they were recorded as caught without ever
being judged. One such sweep scored `hosts.py` at **16** undetected against its true 38.
The tell is in the exit codes: `crap.py` and `leakcheck.py` took zero timeouts and were
the only files that never moved, while `op_secrets.py` took 112. At 60.0 the whole sweep
records zero timeouts and every verdict is a real test outcome. Any baseline or figure
from before 2026-09-12 predates this and is inflated; do not compare against it.

**And still not a nightly CI job — fixing the timeouts narrowed the drift without closing
it.** A scheduled workflow was built and reverted on 2026-09-12. With timeouts gone,
`crap.py` 46, `hosts.py` 38 and `leakcheck.py` 44 are exact on every run; the three
largest files still move by about 2 between **fresh sweeps on riven from an unchanged
tree** — `module_support.py` 31/33, `normalize.py` 163/165, `op_secrets.py` 124/126. A
"may only shrink" ratchet cannot be enforced against a number that moves on its own, so
the gate stays local and advisory.

**Two hypotheses for that residue are already dead**, so don't re-propose them: it is not
I/O (`op_secrets.py` is the most subprocess- and filesystem-heavy file in scope, and
`leakcheck.py` shells out to `git ls-files` and is exact), and it is not
`tests/test_dry_run_all_modules.py` breadth (that correlation held only while
`op_secrets.py` looked stable, which was the timeout artifact). What is left is size: the
three that drift are the three largest. The live suspect is mutmut's stats/coverage phase,
which picks which tests run per mutant.

**Treat `mutation-baseline.json` as riven-relative**, and read its `_drift` key before
touching the three unstable entries: they are pinned to riven's observed *ceiling* rather
than to the last sweep, so re-running does not fail the gate on noise. That is the one
sanctioned exception to "never raise an entry by hand", and `--update-baseline` will
overwrite it with that sweep's figure — restore the ceiling afterwards. Before trusting
any re-baseline, confirm the files you changed score the same twice in a row.

**Stale results are the trap here, and `homelab mutants` handles it — mutmut does not.**
mutmut caches a verdict per mutant and invalidates only on the *mutated source*, its own
config, and tracked non-Python files. A test-only edit matches none of those, so plain
`mutmut run` reprints the previous sweep's numbers after nine minutes of looking busy —
and a test-only edit is what this loop consists of. `homelab mutants` fingerprints
`tests/**/*.py` into `mutants/homelab-test-fingerprint` and discards the tree when it
moves. Two consequences: narrowing with TARGETS only warns (wiping would drop the
untargeted files from the report), and a tree with no fingerprint is treated as fresh, so
delete `mutants/` by hand once after pulling this change.

Scope is `[tool.mutmut].only_mutate` in `pyproject.toml` and is stated nowhere else — the
paths where a wrong answer is *silent* rather than an exception. Widening it is a
deliberate act; the Fabric surface fails loudly and is not worth the runtime. Nothing here
touches Bash, so `lib/utils.sh` and every `scripts/install.sh` stay covered only by their
own subprocess tests.

**`only_mutate` globs whole files — there is no function-level granularity** (patterns
must end in `*` or `.py`, and `do_not_mutate_patterns` is parsed but unused in mutmut
3.8). That is why the public-repo leak check lives in `src/homelab/leakcheck.py` rather
than in `cli.py`: scoping it in place would have meant mutating all 1,000+ lines of
`cli.py`, click wrappers included. Keep that in mind before adding anything to scope — the
unit is the file, so the file has to be worth it.

`mutation-baseline.json` is the same ratchet as `crap-baseline.json`: per-file undetected
counts that may only shrink, never hand-raised, regenerated with
`homelab mutants --update-baseline` only to lock in an improvement. "Undetected" counts
mutants with **no test at all** alongside survivors — a mutant no test exercises is one no
test would have failed on.

## Layout

- `src/homelab/modules/*.py` — local orchestrator for one module.
- `*/scripts/install.sh` — remote installer for the staged bundle.
- `*/templates` — files rendered from Jinja `{{ VAR }}`; `*/configs` — static copies.
- `*/build` — generated output (gitignored).
- `secrets/` — 1Password-backed deploy-time catalog/templates only; no plaintext `.env`.

## Deploy, Disable, and Pause

Three distinct "off/freeze" switches in `hosts.conf` — do not conflate them:

- **`deploy: false`** (host-level feature gate) — removes the host from the module's
  deploy targets: module skipped, running service **never touched**. This is the only
  framework-level gate; a feature-level `enabled:` key is **module-owned** and the
  framework never reads it.
- **`<feature>.paused: true`** (module-wide) — stays deployed, but its managed systemd
  units are **stopped and disabled**; reversible. Supported by `disk-spindown`,
  `apt-upgrade`, `pbs-client-backup`, `zfs-automation`.
- **Per-job `paused: true`** (fine-grained) — pauses one unit while others run (e.g.
  `zfs-automation.replication_jobs.<job>.paused`). Distinct from that job's
  `enabled: false`, which retires it entirely (unit files removed).

Implementation how-to (Python flag read, the `homelab_apply_pause` bash helper, unit-file
semantics, why the `enabled:` spelling of the gate was removed): `deploy-module` skill.

## Shipping and Reboots — Rails

The step-by-step procedures live in the command files, which load on invocation:
`.opencode/command/ship.md` (validate -> dry-run -> deploy/canary -> verify -> commit ->
push -> CI) and `.opencode/command/pve-reboot.md` (with `pve-upgrade/README.md` as the
runbook). These rails apply to any live deploy or reboot, including ad-hoc ones that never
go through a command:

- **Verify the changed value, not just service activity.** Deploy success plus failed
  verification means the host is **diverged** — stop before commit and push, and report
  the observed state.
- **Stage only files belonging to the requested change.** Unrelated dirty files never
  block shipping and are never staged.
- **Reboots are human-authorized, once, against a named plan.** Upgrades are already
  automated (`apt-upgrade` dist-upgrades every PVE node daily, kernel included), so a
  reboot request installs nothing. Never reboot a PVE node that was not named in an
  approved plan — but survey first and ask for that approval once, covering the whole
  roll, rather than re-confirming each node. Never take two *cluster* nodes
  (`ace`/`bray`/`clovis`) down at once. Refuse inside the 02:00 and 08:00 maintenance
  windows. `bray` hosts `riven`, so rebooting it ends the session and empties the shared
  SSH agent — it goes last.

## Coding Style

- Python 3.13; local orchestration should be Python.
- Reuse `HostRegistry`, `HostConnection`, `DeploySession`, and the helpers in
  `src/homelab/module_support.py`; don't invent parallel deployment frameworks.
- Remote installers should source staged `lib/utils.sh` when present and reuse shared
  file-map helpers instead of reimplementing them.
- Bash: portable where shared, quote variables, `$(...)`, localized ShellCheck
  suppressions.
- `hosts.conf`: prefer full systemd calendar expressions (`*-*-* HH:MM:SS`) for
  schedule-like fields.
- No backup, disabled, or timestamped copies inside active config include directories such
  as `/etc/apt/apt.conf.d/`; use `/var/backups/homelab/<module>/` or remove superseded
  files.

Module shape, helper APIs, module-boundary decisions, SSH staging, logging, ShellCheck
examples, and module retirement: the `deploy-module` skill, versioned in-repo at
`.opencode/skill/deploy-module/` alongside the code it describes.

## Inventory and Secrets

- `hosts.conf` is canonical inventory for real managed hosts, not a place to model
  convenience SSH aliases. Keep per-host connection metadata under `config` (`hostname`,
  `user`, `sshkey`, optional `agent`).
- Never commit `.env`, `telegram.env`, rendered secret files, tokens, passwords, or actual
  secret values. `.env.example` and `*.env.tpl.example` placeholders are allowed for
  offline validation.
- Rendered secrets live only in tmpfs under `/dev/shm` — never under the repo or a module
  `build/` directory.
- 1Password `op inject`, tmpfs staging/cache, bootstrap/purge, and runtime `.env`
  boundaries: the `homelab-secrets` skill.
