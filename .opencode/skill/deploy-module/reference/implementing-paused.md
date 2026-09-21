# Implementing `paused`

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
installed — removing them is retirement (`enabled: false`), not pause, and would
break resume.

**Why the gate is spelled `deploy:` and never `enabled:`.** A feature-level `enabled:`
key is module-owned and the framework never reads it — `pbs-client-backup.enabled` is
that module's own flag with its own meaning. The legacy `enabled: false` spelling of the
host-level gate was removed precisely because the same key silently meant two different
things depending on which layer read it first. When adding a new flag, never reuse
`enabled` for anything the framework must interpret.
