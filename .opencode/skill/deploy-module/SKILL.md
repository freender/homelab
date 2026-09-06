---
name: deploy-module
description: Create, modify, or invoke Python deployment modules in the homelab repo — the ./deploy CLI (dry-run and live), hosts.conf, and remote install scripts
---

## When to use

Load this skill when the user asks to:
- Create a new deployment module in the homelab repo
- Modify an existing Python module in `src/homelab/modules/` or a remote `scripts/install.sh`
- Debug deployment issues or dry-run failures
- Work with `hosts.conf`, `src/homelab/`, or the deployment framework
- Invoke `./deploy` itself — dry-run or live, for one module/host or `all all`
- Run or troubleshoot `/ship` — success predicates, verification, stop reasons

This skill lives in the repo it describes. `AGENTS.md` is loaded automatically
alongside it and owns repo layout, build/test commands, deploy/pause semantics,
shipping policy, retirement, and secret rules. This skill owns the **how**: module
internals, helper APIs, and execution detail. Do not restate `AGENTS.md` here.

## Python module shape

Every module in `src/homelab/modules/*.py` follows this flow, via the shared
`run_module_deploy` prologue (`module_support.py`) — do not hand-roll it:

```python
def deploy(root, requested_host, dry_run, force, session):
    return run_module_deploy(
        root,
        requested_host,
        "feature-name",
        session,
        lambda host: deploy_host(root, host, dry_run=dry_run, force=force),
        validate=lambda supported_hosts, hosts: validate(root, hosts),  # optional
    )
```

`run_module_deploy` resolves `supported_hosts`/`hosts`, prints the clean skip when
none apply, runs `validate` if given (uncaught — `execute_module` in `cli.py` already
catches `ValueError` centrally, so a module-local try/except only duplicates that),
then calls `session.run`/`session.finish()`. `validate`'s callback receives both
`supported_hosts` (every host with the feature enabled) and `hosts` (the subset
matching `requested_host`) since modules differ on which one they need to check
(e.g. `pve-backup` validates across all configured hosts even when deploying to
one) — take whichever the module needs and ignore the other. Every module uses
this except `pve-autoinstall`, which drives a single fixed host (the PDM host)
running its own remote sync script rather than per-host `session.run` — a
genuinely different shape, not an oversight.

`simple_root_installer_deploy` (below) is a thin wrapper around this for modules
that have no per-host build directory to render — it only stages `scripts/` and
runs `install.sh`.

## hosts.conf access

Never parse `hosts.conf` ad hoc from modules. Use the repo helpers:
- Python modules: `default_registry(root)`, `HostRegistry.list_hosts()`, `filter_hosts()`, `host_config()`, `feature_config()`, and `has_feature()`.
- CLI checks: `PYTHONPATH=src .venv/bin/python -m homelab.cli hosts list --feature <feature>`.

If a new CLI inventory operation is needed, add it to `src/homelab/cli.py` instead of documenting commands that do not exist.

## Naming conventions

- **Indentation:** 4 spaces, no tabs
- **Globals:** `UPPER_SNAKE_CASE` (e.g., `BUILD_ROOT`, `FORCE_UPDATE`)
- **Functions:** `snake_case` descriptive names (e.g., `render_template`)
- **Booleans:** `true`/`false` strings

## Shared helpers

From `src/homelab/module_support.py` and `src/homelab/deploy.py`:

- `default_registry(root)`
- `prepare_build_dir(build_dir)`
- `render_template(template, output, **context)`
- `diff_many(...)`, `build_files(...)`, `write_file_map(...)`
- `connection_for_host(root, host)`
- `feature_paused(registry, host, feature, default=False)`
- `run_module_deploy(...)` — the shared deploy() prologue (host resolution, skip,
  validate, session.run/finish). Every module's `deploy()` should be a one-line
  call to this.
- `simple_root_installer_deploy(...)` — for a module with no per-host build dir:
  just stages `scripts/` and runs `install.sh` as root. Built on top of
  `run_module_deploy`; prefer it over hand-rolling when there's nothing to render.

## Module boundary

Put logic in the **Python orchestrator** when it needs inventory, templating,
diffing, or a decision made once across hosts. Put it in **`scripts/install.sh`**
when it needs to inspect or mutate live host state (systemd units, installed
packages, device nodes).

Do not split one decision across both — a module that renders a value in Python and
then re-derives it in Bash will drift. Render once, pass it down.

## Remote execution and SSH staging

`HostConnection` (via `connection_for_host`) owns the remote side:

- `prepare_remote_dir(...)` — create/clean the staging dir
- `upload_paths(...)` — push the module bundle
- `upload_shared_libs(...)` — push `lib/utils.sh` + `lib/print.sh`
- `run_remote_installer(...)` — execute `scripts/install.sh` on the host

Rules:
- Stage module bundles in `/tmp/homelab-<module>/`
- Remote `scripts/install.sh` should source staged `lib/utils.sh` when present
- Preserve root-user checks where needed
- Never hardcode host lists — derive from `hosts list --feature ...`

## Implementing `paused`

`AGENTS.md` defines the three off-switches and when each applies. To add module-wide
pause support:

1. **Python side** — read the flag with `feature_paused(registry, host, "<feature>")`
   and pass it into the rendered install bundle (typically as a `PAUSED` variable).
2. **Bash side** — early-exit through the shared helper in `lib/utils.sh`:

```bash
if homelab_apply_pause "$PAUSED" homelab-mymodule.timer homelab-mymodule.service; then
    print_header "My Module Complete (paused)"
    exit 0
fi
```

`homelab_apply_pause` returns **0 when paused** (caller stops) and **1 when not
paused** (caller continues to normal enable logic) — note the inversion versus
ordinary shell truthiness. When paused it runs `systemctl disable --now` on each
named unit, so the units end up both stopped *and* disabled while the module stays
deployed.

Keep unit files installed when paused. Removing them is retirement
(`enabled: false`), not pause, and breaks resume.

## Clearing systemd failed-unit state

A unit left in `systemctl --failed` after a fix is redeployed stays "failed" until
its next successful run or an explicit `reset-failed` — that gap is what
homelab-alerting/vmalert failed-unit checks see. Four shared `lib/utils.sh`
helpers cover this; reach for them before writing `systemctl reset-failed` by hand.
Which one you want depends on whether the redeploy changed anything:
changed content -> `homelab_reload_and_clear_failed`; unchanged content but a
transient fault -> `homelab_recover_failed_units`; unit going away ->
`retire_systemd_unit`.

- **`homelab_reload_and_clear_failed "$changed" unit1 [unit2 ...]`** — the
  standard follow-up to `install_file_map`. Runs `daemon-reload` and clears the
  named units' failed records, but only when the caller's changed flag is
  `true`. **The helper owns the gate — call it unguarded**, not inside another
  `if [[ "$changed" == true ]]`:

  ```bash
  changed=false
  install_file_map || rc=$?
  [[ $rc -eq 0 ]] && changed=true

  homelab_reload_and_clear_failed "$changed" homelab-mymodule.service
  ```

  The gate is load-bearing: an unconditional reset would hide a real ongoing
  failure until the next redeploy. Note it clears failure state without proving
  the fix works — the unit goes from "known failed" to "unknown" until its next
  run. Where an immediate verdict matters, follow it with an explicit
  `systemctl start` and check the result, as `zfs-automation`'s replication
  recovery does.
- **`homelab_mask_unwanted_service unit.service ["reason"]`** — mask a unit that
  should never run on this host (LSB init script with no matching hardware, an
  unwanted distro default) and clear its failed record. Idempotent, and a
  reported no-op when the unit isn't installed. The reason is optional and
  echoed to output — omit it rather than asserting something host-specific you
  haven't verified. Used by `pve-postinstall` and `ubuntu-setup`.
- **`homelab_recover_failed_units unit1 [unit2 ...]`** — for units that fail
  from *transient external* causes (registry rate limits, network blips), where
  a redeploy sees no file change and so the gated helper above does nothing.
  Acts only on units currently in the failed state: resets them (which also
  clears the `StartLimitBurst` limiter that otherwise makes systemd refuse the
  start outright) and then starts them, so the unit's own run decides the
  outcome — transient faults recover, persistent ones fail again immediately
  and stay visible. Healthy units are never touched, and a still-failing unit
  warns rather than failing the deploy.

  Only for units that are cheap, idempotent, and safe to run off-schedule.
  `docker` uses it for `homelab-docker-update.service` (a `docker compose up -d`
  oneshot whose `start.sh` pulls images). Deliberately **not** used by
  `pbs-client-backup` (multi-hour backup) or `apt-upgrade` (a start there means
  running a dist-upgrade at deploy time); those have daily timers that clear a
  stale failure on their next successful run, and keeping a possibly-real
  failure visible beats silencing it. Waits up to `HOMELAB_RECOVER_TIMEOUT`
  seconds (default 300), since a `Type=oneshot` start blocks and oneshot
  disables `TimeoutStartSec` by default.
- **`retire_systemd_unit unit-name /path/to/unit-file`** — stop, disable,
  remove, and clear the failed record for a unit being retired. Returns **0
  when it retired something, 1 when there was nothing to do** (the
  `copy_if_changed` convention). Under `set -e` a bare call therefore aborts
  the installer on the common no-op path — consume the status with `if ...;
  then`, a flag assignment, or an explicit `|| true`. Call it once per unit for
  multi-unit retirements and delete any remaining non-unit files (script,
  textfile-collector output) alongside it; `zfs-automation`'s
  `cleanup_retired_health_check` and both `metrics-exporters` cleanups follow
  that shape.

Two hand-rolled `reset-failed` call sites remain on purpose, both outside this
model: `zfs-automation`'s replication recovery (resets *and* starts, to get a
verdict) and `docker/scripts/rebuild.sh` (not a module installer).

The systemd helpers are covered in `tests/test_safety_regressions.py` and the
file helpers in `tests/test_utils_file_helpers.py`, both running real bash
against a stubbed `systemctl` — extend them when changing helper behavior.

## Test coverage map

Add or update tests when touching these areas.

**Read coverage numbers carefully.** `--cov` reports ~68% overall, but roughly
half of that comes from `test_dry_run_all_modules.py`, which asserts only
`exit_code == 0`. Excluding it, assertion-backed coverage is ~42%. A module can
be "covered" and still render semantically wrong output. When judging whether an
area needs tests, run `pytest --ignore=tests/test_dry_run_all_modules.py --cov`
and use that number.

### Cross-cutting

| Test | Covers |
| --- | --- |
| `tests/test_dry_run_all_modules.py` | Parametrized offline dry-run of every registered module against the real `hosts.conf` (`execute_module(name, "all", True, False)` under `HOMELAB_OFFLINE=1`). This is what `homelab validate` relies on for its per-module dry-run gate — it no longer has its own for-loop. A new module is covered automatically via `MODULES`/`ordered_modules()`; no per-module addition needed. **Smoke only** — it proves a module does not raise, never that its output is correct. Do not treat a module as tested because this passes. |
| `tests/test_render_golden.py` | Golden renders for the **network-critical** modules — `pve-postinstall`, `pve-interface-pinning`, `pve-gpu-passthrough`, `pve-autoinstall`, `keepalived`. A bad render is only discovered after a reboot on a host you can no longer reach. Renders against the real `hosts.conf`, so it also catches inventory drift, and asserts no unsubstituted Jinja placeholders survive. The `keepalived` block is different in kind: its assertions are **cross-host invariants** (shared VRID, unique priorities, symmetric self-excluding unicast peer lists, agreed VIP, `dev` matching `interface`, agreed `advert_int`, per-host healthcheck), because a split-brain VIP is invisible to any single host's own validation. |
| `tests/test_hosts.py`, `tests/test_cli_validate.py` | Inventory parsing and the validate command. |
| `tests/test_build_and_templates.py`, `tests/test_module_fallbacks.py` | Build/render plumbing and module fallback (offline `.example` secret) behavior. |
| `tests/test_leak_check.py`, `tests/test_env_example_check.py` | The public-repo leak check and `.env.example` placeholder check (see `AGENTS.md` § Public Repo Boundary). |
| `tests/test_ssh_helpers.py` | `HostConnection` / staging helpers. |

### `lib/utils.sh` — runs as root on every host

| Test | Covers |
| --- | --- |
| `tests/test_safety_regressions.py` | The **systemd** helpers: `retire_systemd_unit`, `homelab_apply_pause`, `homelab_reload_and_clear_failed`, `homelab_recover_failed_units`, `homelab_mask_unwanted_service`, plus assorted footgun regressions (strict boolean normalizers, unknown-host rejection, tmpfs staging). Harness: `run_utils_snippet` (bash function stub) and `run_recover_snippet` (real on-PATH stub, needed because `timeout` execs the binary and bypasses a shell function). |
| `tests/test_utils_file_helpers.py` | The **file-installation** helpers: `file_needs_update`, `copy_if_changed`, `install_if_changed`, the `backup_and_*` variants, `backup_config`, `prune_backup_history`, `load_file_map`/`mapped_dest`/`mapped_mode`, `install_file_map`, `install_build_file_validated`, `require_env`/`require_file`/`require_dir`, `ensure_timer_state`. Includes a cross-language contract test pinning `module_support.write_file_map` (Python writer) to `load_file_map` (bash reader) — they share no schema, and a delimiter change on either side breaks every module at deploy time. Also holds the regression for the 0=changed / 2=error distinction: these helpers must never report a failed `cp`/`install` as a successful change, because installers feed that status into `homelab_reload_and_clear_failed`. |

### Module-specific

| Test | Covers |
| --- | --- |
| `tests/test_zfs_normalize.py` | `zfs_automation/normalize.py` — validators, dataset-path helpers, snapshot plans and templates, migratable-LXC groups, dynamic-LXC source resolution, `source_private_keys` path confinement, `known_host_refresh` validation. Uses a real `HostRegistry` over a temp `hosts.conf`. This is where to add coverage for anything that turns `hosts.conf` into typed plans. |
| `tests/test_zfs_replication_pause.py` | Pause semantics — per-job `paused` vs `enabled: false` in `zfs-automation`. Imports `normalize_replication_config` from the package's `__init__.py` re-export, not `.replication` directly — keep that export if you touch it. |
| `tests/test_docker_stacks.py`, `tests/test_docker_start.py` | `docker-stacks` orchestration and the `docker` module's `start.sh`. |
| `tests/test_monitoring_config.py`, `tests/test_vmalert_rules.py` | Monitoring config rendering and vmalert rule validity. |
| `tests/test_disk_label_exporter.py`, `tests/test_hba_exporter.py`, `tests/test_reboot_exporter.py` | The three `metrics-exporters` textfile collectors (naming, label identity, behavior). |
| `tests/test_pbs_client_backup.py`, `tests/test_pve_backup.py`, `tests/test_pve_http_boot.py`, `tests/test_pve_notifications.py`, `tests/test_base_packages.py` | Module-specific behavior. |
| `tests/test_apt_upgrade.py` | `apt-upgrade`, the single apt mechanism for the fleet since `apt-security-updates` was archived. Pins `auto_reboot` against live inventory (only the offsite hosts opt in) and `SUPPORTED_TYPES` against every host declaring the feature. |

If a new module can take a host off the network or off SSH — or can desynchronize
a cross-host quorum, VIP, or failover group — it belongs in the golden-render set.

### Known thin spots

Modules with no dedicated test, carried only by the dry-run smoke test:
`ubuntu_setup`, `wsl_conf`, `apcupsd`, `disk_spindown`, `apt_upgrade`,
`ssh_config`, `pve_postinstall_webhook`, and the three `pve_*_patch` wrappers.
`zfs_automation/{access,render,staging}.py` and `op_secrets.py` are likewise
largely unasserted. Prefer adding to these over re-covering well-tested areas.
The ~4,000 lines of active `scripts/install.sh` have no execution coverage at
all — ShellCheck only.

## Output/logging

Use output helpers from `src/homelab/output.py`:
- `print_header "Module Name"` — section header
- `print_action "Doing something"` — action step
- `print_sub "Detail"` — sub-step detail
- `print_ok "Success"` — success message
- `print_warn "Warning"` — recoverable condition
- `print_error "Error"` — hard failure

Keep output operational and short. Exit non-zero on hard failures.

## Error handling and idempotency

- Fail fast on missing required files/secrets/config keys
- Return `0` for "not applicable" module/host skips
- Track host-level failures via framework arrays
- Copy/update only when content changes unless `FORCE_UPDATE=true`

## ShellCheck

Common accepted suppressions: `SC1090` (dynamic source), `SC2086` (intentional
splitting). Suppress nothing else without a reason in the comment.

## CLI invocation reference

`./deploy [--dry-run] <module|all> <host|all>` — positional args are always
`<module> <host>`, both accept `all`. Same signature for dry-run and live; the only
difference is the flag.

```bash
./deploy --dry-run apcupsd ace      # dry-run, one module, one host
./deploy --dry-run all all          # dry-run, every module, every host
./deploy apcupsd ace                # live, one module, one host
./deploy all all                    # live, every module, every host
```

`./deploy all all` without `--dry-run` is the broadest possible live action this repo
can take: every module against every host. Treat requests framed as "deploy
everything"/"deploy all for all modules" as this exact invocation — no `--help`/`cat
deploy` discovery needed.

**`all` is not quite every module.** A `ModuleDefinition` with `include_in_all=False`
is skipped by `ordered_modules()` and so never runs under `deploy all`; it must be
named explicitly. `pve-upgrade` is the only one today, because its deploy action *is*
the mutation (`apt-get dist-upgrade` on the target) rather than config convergence.
It additionally refuses a live run without `--confirm-upgrade`:

```bash
./deploy --confirm-upgrade pve-upgrade ace   # live upgrade, one node
```

Note `--confirm-upgrade` is orthogonal to `--force` (`FORCE_UPDATE=true`, re-copy
unchanged files). Use `all_registered_modules()` rather than `ordered_modules()` for
exhaustive checks that must still cover excluded modules — `tests/test_dry_run_all_modules.py`
does exactly that so `pve-upgrade` keeps its dry-run smoke coverage.

## Shipping (`/ship` pipeline)

`/ship` wraps this CLI in validate -> dry-run -> deploy/canary -> verify -> commit -> push
-> CI. `AGENTS.md` owns the behavior and stop conditions for every step; this skill only
provides the deployment CLI and implementation mechanics used by that pipeline.
