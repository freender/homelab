# Implementing `paused`

`AGENTS.md` defines the three off-switches and when each applies. To add module-wide
pause support:

1. **Orchestrator side** — read the flag with
   `feature_paused(registry, host, "<feature>")` and pass it into the install
   bundle as `PAUSED`, either rendered into `build/<host>/env` or, for a module
   with no build directory, via `env_for_host`.
2. **Installer side** (`scripts/install.py`) — read the flag, then own the branch
   yourself:

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

`systemd.pause()` returns nothing: it only does the stopping, and the caller writes
a plain `if paused:` branch. It leaves the units stopped *and* disabled with their
unit files still installed — removing them is retirement (`enabled: false`,
`systemd.retire_unit`), not pause, and would break resume.

**Why the gate is spelled `deploy:` and never `enabled:`.** A feature-level `enabled:`
key is module-owned and the framework never reads it — `pbs-client-backup.enabled` is
that module's own flag with its own meaning. The legacy `enabled: false` spelling of the
host-level gate was removed precisely because the same key silently meant two different
things depending on which layer read it first. When adding a new flag, never reuse
`enabled` for anything the framework must interpret.
