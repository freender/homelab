---
name: deploy-module
description: Create, modify, retire, or invoke Python deployment modules in the homelab repo — src/homelab/modules/, remote scripts/install.py, hosts.conf, and the ./deploy CLI (dry-run and live). Use when adding or changing a module, debugging a deploy or dry-run failure, running ./deploy for one module/host or all all, running or troubleshooting /ship, or retiring a module. Repo layout, build/test commands, and shipping rails are in AGENTS.md.
---

## Scope

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
| `reference/implementing-paused.md` | Adding or changing pause support (`<feature>.paused`) in a module |

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
runs the installer (`install.py` for `base-packages`/`pve-upgrade`, `install.sh` for
the three `pve-*-patch` modules).

## hosts.conf access

Never parse `hosts.conf` ad hoc from modules. Use the repo helpers:
- Python modules: `default_registry(root)`, `HostRegistry.list_hosts()`, `filter_hosts()`, `host_config()`, `feature_config()`, and `has_feature()`.
- CLI checks: `PYTHONPATH=src .venv/bin/python -m homelab.cli hosts list --feature <feature>`.

If a new CLI inventory operation is needed, add it to `src/homelab/cli.py` instead of documenting commands that do not exist.

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
  Defaults to `installer="scripts/install.sh"`, `interpreter=None` (the
  `pve-*-patch` modules); a Python installer passes
  `installer="scripts/install.py", interpreter="python3"`.

## Module boundary

Put logic in the **Python orchestrator** when it needs inventory, templating,
diffing, or a decision made once across hosts. Put it in **`scripts/install.py`**
when it needs to inspect or mutate live host state (systemd units, installed
packages, device nodes).

Do not split one decision across both — a module that renders a value in Python and
then re-derives it in the installer will drift. Render once, pass it down.

## Remote execution and SSH staging

`HostConnection` (via `connection_for_host`) owns the remote side:

- `prepare_remote_dir(...)` — create/clean the staging dir
- `upload_paths(...)` — push the module bundle
- `upload_python_lib(...)` — push `lib/py/homelab_install/`; called by
  `stage_and_run_remote_installer` for Python installers only (the `pve-*-patch`
  bash installers source no shared library)
- `run_remote_installer(...)` — execute the installer on the host
- `cleanup_remote_dir(...)` — shred and remove the staging dir;
  `stage_and_run_remote_installer` calls it in a `finally`, so a module never
  cleans up its own bundle

Rules:
- Stage module bundles in `/tmp/homelab-<module>/` — the cleanup refuses any other
  prefix, and the bundle is gone after the run (never reference it from a unit)
- Preserve root-user checks where needed
- Never hardcode host lists — derive from `hosts list --feature ...`

### Bash or Python installer

A module declares one installer and **the `.py` suffix is the entire switch.**
`stage_and_run_remote_installer` reads it (`is_python_installer`) and from that alone
uploads `lib/py/homelab_install/` and prepends `{remote_root}/lib/py` to `PYTHONPATH`.
There is no second flag; a Python installer named without the suffix is staged without
its library. The module must also pass `interpreter="python3"`.

`lib/py/homelab_install/` is the stdlib-only shared installer library — the only one;
the bash `lib/utils.sh`/`lib/print.sh` are gone (`homelab-ops#38`). Hermetic in one direction:
it must never import from `src/homelab/`, while `src/homelab/` and `tests/` may import
it. `./validate` compiles and Ruff-lints `lib/py` and every `*/scripts/install.py`
(`python_lint_targets` in `cli.py`), and coverage/CRAP score it like any other
package. Every module is on `install.py` except the three `pve-*-patch` modules,
whose standalone `install.sh` stays (homelab-ops#35); a module never ships both.

## Implementing `paused`

Orchestrator reads `feature_paused(...)` and passes `PAUSED` down; the installer
branches on it. **The silent trap:** `env.flag` reads `build/<host>/env` and
`env.deploy_flag` reads the process environment — pick the wrong one and a module
with no env file gets the default forever, so a paused host keeps acting. Full
steps and the `deploy:`-vs-`enabled:` gate rationale:
`reference/implementing-paused.md`.

## Clearing systemd failed-unit state

Installers that manage systemd units should use the `homelab_install.systemd`
helpers rather than hand-rolling `systemctl reset-failed`. Pick by what the redeploy
did: changed content -> `daemon_reload` + `reset_failed`, gated on the change;
unchanged content but a transient fault -> `recover_failed`; a unit that should never
run here -> `mask`; a unit going away -> `retire_unit`.

Full semantics, the load-bearing gate, return values, and which modules
deliberately opt out: `reference/systemd-failed-state.md`.

## Tests

Add or update tests when touching a module. The coverage map — which test owns
which area, the golden-render set for network-critical modules, and the known
thin spots — is in `reference/test-coverage.md`. Note that headline `--cov`
numbers are inflated by the dry-run smoke test; that file explains how to read
them.

## Output and error handling

Use the `print_*` helpers in `src/homelab/output.py` rather than bare `print`; keep
output operational and short. Fail fast on a missing required file, secret, or config
key and exit non-zero; return `0` for a "not applicable" module/host skip; copy only
when content changes unless `FORCE_UPDATE=true`.

## ShellCheck

Common accepted suppressions: `SC1090` (dynamic source), `SC2086` (intentional
splitting). Suppress nothing else without a reason in the comment.

## CLI invocation reference

`./deploy [--dry-run] <module|all> <host|all>` — positional args are always
`<module> <host>`, both accept `all`. Same signature for dry-run and live; the only
difference is the flag.

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

