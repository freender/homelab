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
homelab mutants                                 # sweep the scoped core, then gate
homelab mutants --no-run                        # re-score the last sweep without redoing it
homelab mutants 'homelab.hosts.*'               # narrow further than the configured scope
mutmut show <mutant-name>                       # the exact surviving diff
```

**Not a `./validate` step and not in CI:** a sweep is tens of minutes. Run it when you
change a scoped file, then fix or re-baseline. `mutants/` is a gitignored working copy of
the repo; results accumulate there across runs.

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
pure-logic paths where a wrong answer is *silent* rather than an exception. Widening it is
a deliberate act; the Fabric and subprocess surfaces fail loudly and are not worth the
runtime. Nothing here touches Bash, so `lib/utils.sh` and every `scripts/install.sh` stay
covered only by their own subprocess tests.

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
- **Reboots are human-authorized per node.** Upgrades are already automated (`apt-upgrade`
  dist-upgrades every PVE node daily, kernel included), so a reboot request installs
  nothing. Never reboot a PVE node without explicit confirmation of that specific node, and
  never take two *cluster* nodes (`ace`/`bray`/`clovis`) down at once. Refuse inside the
  02:00 and 08:00 maintenance windows. `bray` hosts `riven`, so rebooting it ends the
  session and empties the shared SSH agent — it goes last.

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
