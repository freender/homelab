# Homelab Agent Guide (AGENTS.md)

Python-orchestrated automation for a Proxmox homelab. Python + Bash + YAML; inventory in
`hosts.conf`; local orchestration in `src/homelab/`; shared remote library in
`lib/py/homelab_install/`. Pattern: a Python module builds and stages files, then runs a
remote `scripts/install.py` (the three `pve-*-patch` modules keep a bash `install.sh`).

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
  `<module>/scripts/install.py` (or `install.sh`) before editing.
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
shellcheck -S warning pve-zfs-large-block-patch/scripts/install.sh
find . -name '*.sh' -not -path './.bin/*' -exec shellcheck -S warning {} +   # repo root only
yq '.' hosts.conf >/dev/null                          # apt's yq (kislyuk/yq, jq syntax) — no `eval`, no mikefarah-style paths
```

`./validate` runs Python compile, Ruff, Pytest, the CRAP gate, `hosts.conf` parse
validation, the inventory/module cross-check, the leak check, ShellCheck, and module
dry-runs — the same set CI runs on push/PR to `main` (`.github/workflows/validate.yml`).
Ruff and Pytest are skipped with a warning when missing,
so run it from the repo `.venv` for true CI parity. CI and the venv install the same
exact versions from `constraints.txt`
(`.venv/bin/python -m pip install -c constraints.txt '.[dev,mutation]'`), and
`tests/test_dev_constraints.py` fails when the venv drifts from it — never install or
upgrade a dev tool without `-c`. Change a version by regenerating the file (its header
says how), not by hand-editing one line. Run the targeted checks
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

`crap-baseline.json` grandfathers the functions that were already over 10. It may only
shrink, never grow.

- New or moved code is held to 10 from its first commit — it is never in the baseline.
  Note the key is `filename::name`, not line number, so *moving* a function counts as new.
- A baselined function that gets *worse* fails too (0.5 tolerance for coverage noise).
- Never hand-add or hand-raise an entry. Regenerate with
  `homelab crap --update-baseline` only to lock in an improvement.

**The remaining baseline is frozen — do not refactor to clear an entry.** The gate's value
is holding *new* code to 10, and that is fully intact above. The entries left are
validators and env-loaders that are case-heavy because the inventory they validate
genuinely has many cases; splitting them further scatters the logic without reducing real
complexity. The one previous clearing pass that reached this floor (`5b833e7`) also
changed three error-precedence orderings — evidence that metric-driven splitting of these
particular functions stops being behaviour-preserving. Treat a `cleared` notice from
`validate` as informational, not a chore.

Still regenerate when an entry clears *incidentally* during real work — the ratchet only
shrinks either way. Known cost of not regenerating: a stale entry keeps its old recorded
score, so a function that organically improved could creep back up to that score without
failing. Bounded, since it can never exceed where it already was.

### Mutation Testing (`homelab mutants`)

The check for the hole the CRAP gate names above. mutmut rewrites one expression at a
time — `continue` to `break`, `>` to `>=` — and reruns the tests that touch it. A mutant
the suite still passes is a behaviour **nothing asserts**, which coverage cannot see.

```bash
.venv/bin/python -m pip install -c constraints.txt '.[mutation]'   # separate extra, not in dev
M=".venv/bin/python -m homelab.cli mutants"     # see the PYTHONPATH note below
PYTHONPATH=src $M                               # sweep the scoped core, then gate
PYTHONPATH=src $M --no-run                      # re-score the last sweep without redoing it
PYTHONPATH=src $M 'homelab.hosts.*'             # narrow further than the configured scope
PYTHONPATH=src .venv/bin/python -m homelab.cli survivors op_secrets [function]  # what changed
```

Working the backlog — reading survivors, telling a real gap from an equivalent mutant, the
test shapes that kill them — is the `mutation-triage` skill. `mutmut show` cannot resolve a
mutant in this tree; `homelab survivors` is the replacement.

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
time.** mutmut puts a CPU-seconds cap on each mutant and **scores a mutant that hits it as
killed** (SIGXCPU, exit `-24`; `DETECTED_EXIT_CODES` mirrors mutmut here deliberately, on
the theory that a hang is a detection). At the stock multiplier that cap fired on hundreds
of mutants that were not hanging, inflating every score.

**Run it serially. `--max-children` defaults to 1, and `--update-baseline` refuses
anything else.** Every child shares the *same* `mutants/` working tree, so a mutant that
writes under it makes a **different** child's test fail, and that unrelated mutant is
recorded as killed. Parallel sweeps are therefore biased *low*. Use `--max-children 8` to
explore quickly, never to judge.

**Measure a file twice before ratcheting it.** Every baseline entry was reproduced on two
independent fresh serial sweeps, so `mutation-baseline.json` is exact and carries no drift
tolerance — a sweep that disagrees is reporting a real change or a parallel run, not noise.
The error is only safe in one direction: an entry that is too high reports as "improved",
one that is too low fails the gate for everyone afterwards. Both artifacts that once
inflated these figures, the five dead hypotheses for the drift, and why this is still not a
nightly CI job: `.opencode/skill/mutation-triage/reference/measurement-history.md`. Do not
compare against any figure older than `4f1048b`.

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
touches Bash, so the three patch modules' `install.sh` — the only bash installers left,
after `lib/utils.sh`, `lib/print.sh` and the four `remove*.sh` were deleted in
homelab-ops#38 — stay covered only by their own subprocess tests.

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
- `*/scripts/install.py` — remote installer for the staged bundle (`install.sh` for the
  three `pve-*-patch` modules).
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
- Remote installers are Python on `lib/py/homelab_install/` and reuse its file, package
  and systemd helpers instead of reimplementing them.
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
