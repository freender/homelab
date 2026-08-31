# apt-security-updates

Enables `unattended-upgrades` scoped to **Debian security only** on hosts whose
full `dist-upgrade` is deliberately manual — the four Proxmox nodes.

```bash
./deploy --dry-run apt-security-updates ace
./deploy apt-security-updates all
```

## Why

The PVE nodes were the only hosts with no automated patching at all. They are
absent from `apt-upgrade` on purpose: it runs `apt-get -y dist-upgrade` on a
timer, and unattended-upgrading `pve-manager`, the kernel or ZFS on a hypervisor
is how you lose a cluster. The consequence was that nothing patched them and
nothing said so — all four had accumulated 20 pending Debian security updates
when this module was written (2026-08-16).

Splitting by origin resolves the tension:

| Stream | Origin | Applied by |
| --- | --- | --- |
| Debian security | `o=Debian, l=Debian-Security` | this module, automatically |
| Proxmox | `o=Proxmox` | `pve-upgrade`, by hand, monthly |

## What it installs

| File | Purpose |
| --- | --- |
| `/etc/apt/apt.conf.d/52homelab-security-updates` | Origin scope, blacklist, no auto-reboot |
| `/etc/apt/apt.conf.d/20homelab-auto-upgrades` | Enables the `apt-daily*` timers that run it |

Both are static — the origin pattern uses APT's own `${distro_codename}`
expansion rather than a value rendered at deploy time, so it keeps working
across a Debian major upgrade without this module knowing the codename.

## The two things that make this safe

**`#clear` before each list.** APT list assignment *appends*; it does not
replace. Without `#clear Unattended-Upgrade::Origins-Pattern`, the packaged
`50unattended-upgrades` defaults would remain active alongside ours and this
module would *widen* the scope instead of narrowing it. Silent, and the file
would still look correct.

**`verify_origins_scope` in `install.sh`.** Because that failure is silent, the
installer does not trust the file it just wrote — it runs `apt-config dump
Unattended-Upgrade::Origins-Pattern` and asserts the *resolved* policy contains
`Debian-Security` and does **not** match `proxmox`, `stable-updates` or
`backports`. Backports matters specifically: `metrics-exporters` enables it on
these hosts for `prometheus-smartctl-exporter`. If the check fails the deploy
fails, leaving the host unconfigured rather than silently over-scoped.

## Mutually exclusive with `apt-upgrade`

Enabling both on one host would leave this module's narrow scope in place while
`homelab-apt-dist-upgrade.timer` ignored it entirely. That is rejected twice:

- `validate()` fails if any host lists both features in `hosts.conf`, so
  `./validate` and CI catch it before a deploy.
- `install.sh` re-checks live systemd state, which also catches a leftover timer
  from an earlier deploy that `hosts.conf` no longer describes.

## Pause

`apt-security-updates.paused: true` disables `apt-daily-upgrade.timer`, the unit
that actually invokes `unattended-upgrades`. Config stays in place and the
package index still refreshes via `apt-daily.timer`; the host simply stops
installing anything on its own. Reversible by removing the flag and redeploying.

## Scope

Currently `ace`, `bray`, `clovis`, `osiris`.

`arc` and `xur` are the same case — they carry `pve-upgrade` and not
`apt-upgrade`, so they are also patched only by hand — and are deliberately not
included yet. They are LXC guests, so their updates are userspace-only and
lower risk still; adding them is a one-line change per host once this has proven
itself on the nodes.

## Verifying on a host

```bash
# Resolved policy — should list only the Debian security origin
apt-config dump Unattended-Upgrade::Origins-Pattern

# What it would actually do right now
unattended-upgrade --dry-run --debug 2>&1 | tail -20

# The timers that run it
systemctl status apt-daily.timer apt-daily-upgrade.timer

# History
journalctl -u unattended-upgrades --since '7 days ago'
cat /var/log/unattended-upgrades/unattended-upgrades.log
```

## Related

- `pve-upgrade/README.md` — the monthly manual runbook for the Proxmox stream.
- `vmalert-rules/configs/apt-updates.yml` — `SecurityUpdatesPending` (safety net
  for this module) and `ProxmoxUpdatesAvailable` (the weekly digest).
