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

Every module in `src/homelab/modules/*.py` follows this flow:

```python
def deploy(root, requested_host, dry_run, force, session):
    registry = default_registry(root)
    supported_hosts = registry.list_hosts(feature="feature-name")
    hosts = registry.filter_hosts(requested_host, supported_hosts)
    validate(root)
    session.run(lambda host: deploy_host(root, host, dry_run=dry_run, force=force), hosts)
    return 0 if session.finish() else 1
```

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
- `simple_root_installer_deploy(...)` — the standard path for a module that just
  stages a bundle and runs `install.sh` as root; prefer it over hand-rolling.

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

## Test coverage map

Add or update tests when touching these areas:

| Test | Covers |
| --- | --- |
| `tests/test_render_golden.py` | Golden renders for the four **network-critical** modules — `pve-postinstall`, `pve-interface-pinning`, `pve-gpu-passthrough`, `pve-autoinstall`. A bad render is only discovered after a reboot on a host you can no longer reach. Renders against the real `hosts.conf`, so it also catches inventory drift, and asserts no unsubstituted Jinja placeholders survive. |
| `tests/test_zfs_replication_pause.py` | Pause semantics — per-job `paused` vs `enabled: false` in `zfs-automation`. |
| `tests/test_hosts.py`, `tests/test_cli_validate.py` | Inventory parsing and the validate command. |
| `tests/test_build_and_templates.py`, `tests/test_module_fallbacks.py` | Build/render plumbing and module fallback behavior. |
| `tests/test_pbs_client_backup.py`, `tests/test_pve_backup.py`, `tests/test_pve_http_boot.py`, `tests/test_docker_start.py`, `tests/test_ssh_helpers.py` | Module-specific behavior. |

If a new module can take a host off the network or off SSH, it belongs in the
golden-render set.

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

## Shipping (`/ship` pipeline)

`/ship` wraps this CLI in validate -> dry-run -> deploy -> verify -> commit -> push.
**When** to use it, the module risk tiers, and the escalation ladder live in
`AGENTS.md` ("Shipping Strategy"). This section covers the execution detail.

### Writing a success predicate

Every ship run must state, before deploying, a specific assertion the change makes
true on the host. `systemctl is-active` alone is never sufficient — a service runs
happily on the old config. Assert on the *changed value*:

```bash
systemctl show <unit> -p ExecStart          # new flag/arg present
grep -q '<new value>' /etc/<path>           # rendered config landed
systemctl list-timers --all '<timer>'       # next elapse matches new schedule
systemctl is-enabled <unit>                 # paused: true -> disabled + inactive
curl -sf localhost:<port>/metrics | grep -q '<metric>'   # exporter actually serving
docker inspect -f '{{.Config.Image}}' <ctr> # container on the new image
```

Capture the same value *before* deploying. Without a pre-state there is nothing to
compare against, and "verified" degrades into "the installer exited 0".

For renames, retirements, and path migrations the predicate must also assert the
**negative** — old unit gone, old path absent. A predicate that only checks the new
state passes while the host still carries the old one.

### Stop-reason playbook

| Report says | Action |
| --- | --- |
| Precondition failed | Clean the working tree or name the module explicitly, then re-run. |
| No predicate statable | The change isn't verifiable — a design gap. Fix before shipping. |
| Validate/dry-run failed after 3 auto-fixes | Real problem. Go back to editing; don't re-run `/ship`. |
| Transient exhausted its retry | Check host reachability, then re-run — idempotent up to the deploy step. |
| **Host diverged** | Highest priority. Host matches neither git nor pre-state. Decide re-deploy from HEAD vs. manual revert before touching anything else. |
| CI red after push | `./validate` claims CI parity, so red means flake or environment drift. Investigate; the change is already live. |

Auto-fix boundary: before deploy (validate, dry-run) everything is repo-only and
reversible, so fixing and re-running is safe. Once deploy has touched a host, never
auto-fix — recover and report.
