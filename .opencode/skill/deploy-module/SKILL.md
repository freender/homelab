---
name: deploy-module
description: Create, modify, or invoke Python deployment modules in the homelab repo — the ./deploy CLI (dry-run and live), hosts.conf, and remote install scripts
---

## When to use

Load this skill when the user asks to:
- Create a new deployment module in the homelab repo
- Modify an existing Python module in `src/homelab/modules/` or a remote `scripts/install.py`
- Debug deployment issues or dry-run failures
- Work with `hosts.conf`, `src/homelab/`, or the deployment framework
- Invoke `./deploy` itself — dry-run or live, for one module/host or `all all`
- Run or troubleshoot `/ship` — success predicates, verification, stop reasons
- Retire a module (`reference/module-retirement.md`)

This skill lives in the repo it describes. `AGENTS.md` is loaded automatically
alongside it and owns repo layout, build/test commands, the three off-switches, the
shipping and reboot rails, and secret rules. This skill owns the **how**: module
internals, helper APIs, and execution detail. Do not restate `AGENTS.md` here.

## Reference files (read on demand, not up front)

| File | When |
|---|---|
| `reference/systemd-failed-state.md` | An installer manages systemd units — clearing, recovering, masking, or retiring failed state |
| `reference/test-coverage.md` | Adding or updating tests, or judging whether an area is genuinely covered |
| `reference/module-retirement.md` | Removing a module from the framework, or clearing `./validate`'s orphan-module warning |

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
  just stages `scripts/` and runs the installer as root. Built on top of
  `run_module_deploy`; prefer it over hand-rolling when there's nothing to render.
  Defaults to `installer="scripts/install.sh"`, `interpreter=None`; a ported
  module passes `installer="scripts/install.py", interpreter="python3"`.

## Module boundary

Put logic in the **Python orchestrator** when it needs inventory, templating,
diffing, or a decision made once across hosts. Put it in **`scripts/install.py`**
when it needs to inspect or mutate live host state (systemd units, installed
packages, device nodes).

Do not split one decision across both — a module that renders a value in Python and
then re-derives it in Bash will drift. Render once, pass it down.

## Remote execution and SSH staging

`HostConnection` (via `connection_for_host`) owns the remote side:

- `prepare_remote_dir(...)` — create/clean the staging dir
- `upload_paths(...)` — push the module bundle
- `upload_shared_libs(...)` — push `lib/utils.sh` + `lib/print.sh`, plus
  `lib/py/homelab_install/` when `include_python=True`
- `run_remote_installer(...)` — execute the installer on the host

Rules:
- Stage module bundles in `/tmp/homelab-<module>/`
- Preserve root-user checks where needed
- Never hardcode host lists — derive from `hosts list --feature ...`

### Bash or Python installer

A module declares one installer and **the `.py` suffix is the entire switch.**
`stage_and_run_remote_installer` reads it (`is_python_installer`) and from that alone
uploads `lib/py/homelab_install/` and prepends `{remote_root}/lib/py` to `PYTHONPATH`.
There is no second flag; a Python installer named without the suffix is staged without
its library. The module must also pass `interpreter="python3"`.

`lib/py/homelab_install/` is the stdlib-only shared library that replaces
`lib/utils.sh` for ported modules (`homelab-ops#30`/`#31`). Hermetic in one direction:
it must never import from `src/homelab/`, while `src/homelab/` and `tests/` may import
it. `./validate` compiles and Ruff-lints `lib/py` and every `*/scripts/install.py`
(`python_lint_targets` in `cli.py`), and coverage/CRAP score it like any other
package. Porting rule: `install.py` added and `install.sh` deleted in the **same
commit** — never both present.

## Implementing `paused`

`AGENTS.md` defines the three off-switches and when each applies. To add module-wide
pause support:

1. **Orchestrator side** (both flavors) — read the flag with
   `feature_paused(registry, host, "<feature>")` and pass it into the install
   bundle as `PAUSED`, either rendered into `build/<host>/env` or, for a module
   with no build directory, via `env_for_host`.
2. **Installer side** — depends on whether the module is ported.

**Ported (`install.py`).** Read the flag, then own the branch yourself:

```python
if env.flag(ctx, "PAUSED"):            # from build/<host>/env
    systemd.pause(ctx, "homelab-mymodule.timer", "homelab-mymodule.service")
    log.header("mymodule paused")
    return
```

Use `env.deploy_flag` instead when the module has no build directory and `PAUSED`
arrives on the process environment (`simple_root_installer_deploy`) — `pve-upgrade`
is the example, and it just returns, having no units to stop. **Picking the wrong
one of the two is silent:** `env.flag` on a module with no env file returns the
default forever, so a paused host would keep acting.

**Unported (`install.sh`).** Early-exit through the shared helper:

```bash
if homelab_apply_pause "$PAUSED" homelab-mymodule.timer homelab-mymodule.service; then
    print_header "My Module Complete (paused)"
    exit 0
fi
```

`homelab_apply_pause` returns **0 when paused** (caller stops) and **1 when not
paused** — note the inversion versus ordinary shell truthiness. `systemd.pause()`
deliberately does **not** reproduce that return: it only does the stopping, and the
caller writes a plain `if paused:`, so there is no inverted code to misread.

Either way the units end up stopped *and* disabled with their unit files still
installed — removing them is retirement, not pause, and would break resume.

Keep unit files installed when paused. Removing them is retirement
(`enabled: false`), not pause, and breaks resume.

**Why the gate is spelled `deploy:` and never `enabled:`.** A feature-level `enabled:`
key is module-owned and the framework never reads it — `pbs-client-backup.enabled` is
that module's own flag with its own meaning. The legacy `enabled: false` spelling of the
host-level gate was removed precisely because the same key silently meant two different
things depending on which layer read it first. When adding a new flag, never reuse
`enabled` for anything the framework must interpret.

## Clearing systemd failed-unit state

Installers that manage systemd units should use the shared `lib/utils.sh` helpers
rather than hand-rolling `systemctl reset-failed`. Pick by what the redeploy did:
changed content -> `homelab_reload_and_clear_failed`; unchanged content but a
transient fault -> `homelab_recover_failed_units`; a unit that should never run
here -> `homelab_mask_unwanted_service`; a unit going away -> `retire_systemd_unit`.

Full semantics, the load-bearing gate, return-code conventions, and which modules
deliberately opt out: `reference/systemd-failed-state.md`.

## Tests

Add or update tests when touching a module. The coverage map — which test owns
which area, the golden-render set for network-critical modules, and the known
thin spots — is in `reference/test-coverage.md`. Note that headline `--cov`
numbers are inflated by the dry-run smoke test; that file explains how to read
them.

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
-> CI. `.opencode/command/ship.md` owns the behavior and stop conditions for every step
(`AGENTS.md` keeps only the rails that outlive the command); this skill provides the
deployment CLI and implementation mechanics used by that pipeline.
