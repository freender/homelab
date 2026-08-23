# homelab/pve-postinstall-webhook

Closes the loop after a PDM (Proxmox Datacenter Manager) automated install: once
a PVE node finishes installing, this module triggers `./deploy all <host>`
against it automatically, so a rebuilt/new node reaches its full `hosts.conf`
config without a manual step.

Deployed only to `arc` (the PDM host itself, `10.0.0.50`).

## Flow

1. **`pve-autoinstall`** (separate module) pushes prepared answers to PDM,
   including `post-hook-base-url` from `hosts.conf`
   (`arc.features.pve-autoinstall.post_hook_base_url`, currently
   `https://10.0.0.50:8443` — PDM's own API base).
2. A node PXE/ISO-boots, fetches its answer from PDM, and installs. On first
   boot the Proxmox auto-installer POSTs a completion report to
   `<post-hook-base-url>/api2/json/auto-install/installations/<uuid>/post-hook`.
   **This must be PDM's own base URL** — PDM appends that path itself. See
   "Known failure mode" below.
3. PDM records the installation as `status: "finished"` in its own state
   (`GET /api2/json/auto-install/installations`). This module does not
   receive that POST directly.
4. `homelab-pdm-installation-watch.timer` polls that PDM endpoint every 60s
   (`homelab-pdm-installation-watch.service`, oneshot). For each install with
   `status == "finished"` not already recorded in
   `/var/lib/homelab-postinstall-webhook/state/<uuid>.queued`, it matches the
   install to a `hosts.conf` host (`answer-id`, `dmi.system.uuid`, or a mgmt
   MAC against `pve-autoinstall.answer_name` / `dmi_uuid` / `mgmt_mac`),
   writes an event to `/var/lib/homelab-postinstall-webhook/events/`, and
   queues the deploy via `systemd-run`.
5. **`homelab-postinstall-deploy <host> [event_file]`** does the actual work:
   waits for SSH readiness (`pve-cluster` active + `pvesh get /version` for
   PVE hosts), `git pull --ff-only` in `$REPO_DIR` (`/root/homelab`, needs a
   loaded SSH key — see "`homelab-postinstall-deploy`'s root SSH agent"
   below),
   refreshes the PDM remote's trust/token for standalone PVE hosts
   (`homelab-pdm-refresh-remote`, rotates the PVE API token if the existing
   PDM remote fails a version check), then runs the deploy:
   - Cluster PVE nodes (ace/bray/clovis): single `./deploy all <host>` pass.
   - Standalone PVE nodes (osiris): gated two-pass — deploy, reboot, wait for
     SSH again, deploy again.

There is a second, parallel HTTP path: `homelab-postinstall-webhook.py`
listens on `:9443` for `POST /pve-installed` with a JSON body containing
`{"token": WEBHOOK_TOKEN, ...}`, running the same host-matching and
`enqueue_deploy()` logic. This exists as a manual/alternate trigger — it is
**not** what PDM's own `post-hook` calls (see step 2); PDM must call its own
API base, not this receiver.

## Known failure mode: `post_hook_base_url` must be PDM's own base

PDM appends its API path to `post_hook_base_url`, producing
`<base>/api2/json/auto-install/installations/<uuid>/post-hook`. Pointing it at
the `:9443` receiver instead (`http://10.0.0.50:9443/pve-installed`) nests
PDM's whole path under `/pve-installed` and 404s — the auto-installer's own
post-hook step then reports "Post-installation hook failed", and PDM's
install record is stuck at `status: "in-progress"` forever. The node itself
finishes installing fine (it only fails to *report* that it finished), so a
node in this state is often already fully booted and reachable over SSH while
PDM's UI still shows it mid-install.

This is a real failure mode that happened once (`ace`'s Aug 2026 rebuild) and
was fixed in `hosts.conf` (commit `13d0cbe`, `post_hook_base_url:
https://10.0.0.50:8443`). Confirm the fix is live in PDM's own record before
assuming a stuck install is this bug recurring:

```bash
ssh arc 'set -a; source /etc/homelab-postinstall-webhook/env; set +a
curl -sk -H "Authorization: PDMAPIToken ${PDM_TOKEN_ID}:${PDM_TOKEN_SECRET}" \
  https://127.0.0.1:8443/api2/json/auto-install/prepared/<answer_name> \
  | python3 -m json.tool'
```

Look for `"post-hook-base-url": "https://10.0.0.50:8443"` (PDM's own base, not
`:9443`).

**A stuck `in-progress` record CAN be completed after the fact — PDM 1.1.7
has no admin API for it (`/api2/json/auto-install/installations` only
supports `GET` on the collection; there is no `GET`/`DELETE`/`PUT` on an
individual `<uuid>`), but the real per-install completion endpoint is
reachable directly and does work.** Confirmed hands-on completing `ace`'s
stuck Aug 2026 install (uuid `24339bf2-…`) after the fact:

1. Each installation's state is a flat JSON file on `arc` at
   `/var/lib/proxmox-datacenter-manager/automated-installations/<uuid>.json`
   (owner `www-data:www-data`, mode `640` — read as root). It carries a
   `post-hook-token` field — the per-install secret the post-hook route
   actually authenticates against. This is *not* exposed by the
   `GET .../auto-install/installations` API response; only the flat file has
   it. **My first attempt 404'd** (`installation <uuid> not found`) because I
   POSTed without this token — that response is misleading, it means "auth
   failed", not "record gone".
2. `POST https://<pdm-host>:8443/api2/json/auto-install/installations/<uuid>/post-hook`
   with a JSON body matching the [Post-installation webhook JSON
   example](https://pve.proxmox.com/wiki/Automated_Installation#Post-installation_webhook_JSON_example)
   schema, `"token"` set to that `post-hook-token` value, and the rest of the
   fields built from the live host's real current state (machine-id, SSH
   host keys, disks, NICs, DMI — DMI can be copied verbatim from the
   `info.dmi` block already in the historical installation record). Returns
   `200 OK`, and the record's `status` flips to `"finished"` both via the API
   and in the state file.
3. This immediately makes `homelab-pdm-installation-watch` queue a real
   deploy for the host on its next poll (same as a live completion would) —
   expect that side effect, don't be surprised by it. If the host was already
   fully deployed by other means, this is a harmless idempotent re-run.

Only use this for a record you've independently confirmed is real — a
genuinely completed install whose post-hook failed (see "Known failure mode"
above). It replays truthful data about a real host through PDM's own
documented API; it does not forge a fake completion.

Schema notes from that replay (the wiki's example does not exactly match what
the server enforces):
- `ssh-public-host-keys` is an object keyed by `ecdsa`/`ed25519`/`rsa`
  (**not** `ssh-ed25519`/etc., and **not** an array).
- A network interface's `address` field must be omitted entirely (not an
  empty string) when the interface has no address, e.g. it was enslaved into
  a bridge post-install.
- `filesystem` is not the plain `disk-setup.filesystem` value (`"zfs"` is
  rejected) — it takes the RAID mode too, e.g. `"zfs (RAID0)"`.

Check `proxmox-datacenter-manager-docs`
(`/usr/share/doc/proxmox-datacenter-manager/html/_sources/automated-
installations.rst.txt` on `arc`) and the [Proxmox PDM
changelog](https://pve.proxmox.com/wiki/Automated_Installation) when PDM is
upgraded, in case a future release exposes this as a real admin action
instead of a state-file dig.

## Manual recovery

If a real install is stuck (watcher keeps logging `queued=0` for a node you
know finished), on `arc` as root:

```bash
# Force an immediate poll instead of waiting up to 60s:
systemctl start homelab-pdm-installation-watch.service
journalctl -u homelab-pdm-installation-watch.service -n 50

# If a stale state file is blocking a re-match:
rm /var/lib/homelab-postinstall-webhook/state/<uuid>.queued
systemctl start homelab-pdm-installation-watch.service

# Skip PDM entirely and run the deploy trigger directly (safe/idempotent —
# same script the watcher/webhook would have queued):
/usr/local/sbin/homelab-postinstall-deploy <host>
```

Check what PDM currently has recorded:

```bash
ssh arc 'set -a; source /etc/homelab-postinstall-webhook/env; set +a
curl -sk -H "Authorization: PDMAPIToken ${PDM_TOKEN_ID}:${PDM_TOKEN_SECRET}" \
  https://127.0.0.1:8443/api2/json/auto-install/installations | python3 -m json.tool'
```

## `homelab-postinstall-deploy`'s root SSH agent

`homelab-postinstall-deploy` needs two identities to actually do anything
useful: the PVE infra key (`SSH -> ace/bray/clovis/osiris`, also used by
`homelab-pdm-refresh-remote`) and the main homelab key (`git pull` from
`git@github.com:freender/homelab.git`). Both are passphrase-protected and
must be decrypted into a persistent root SSH agent (`/root/.ssh/agent.sock`)
non-interactively, since `arc` is headless with no human session.

**This was a real gap, found and fixed 2026-08-23.** A root SSH agent +
1Password-backed loader already existed on `arc` (`homelab-ssh-agent.service`
+ `homelab-op-ssh-load.service`/`.timer` calling `/root/.local/bin/
addhomelabkeys` → `op-ssh-add`) — but it was hand-built directly on the host,
**not present anywhere in this repo**, so a rebuild of `arc` would not have
reproduced it. Worse, `addhomelabkeys` was deliberately scoped to load only
the `pve` key (comment: "arc only needs the Homelab-Infra (pve) key... Main
and offsite keys are intentionally not loaded"), predating `git pull` being
added to `homelab-postinstall-deploy`'s flow — so `git pull --ff-only` failed
every time a real post-install deploy ran, silently, since `arc`'s copy of
this repo was consequently ~4 months stale by the time this was caught.

The fix, now deployed via this module (`./deploy pve-postinstall-webhook
arc`) so a rebuild reproduces it: `addhomelabkeys` loads **both** `main` and
`pve`. No new secret was needed — `arc`'s existing deploy-scoped 1Password
service account (`/root/.config/op/service-account-token`, `Homelab` vault)
already has read access to both `SSH Key - Homelab` and `SSH Key -
Homelab-Infra` items (verified directly: `op read
"op://Homelab/SSH Key - Homelab/private key"` succeeds from `arc`), and
correctly cannot read `SSH Key - Offsite` — `op-ssh-add`'s forbidden-item
guard confirms this before loading anything. This mirrors the same
`op://Homelab/...` refs and loader pattern as riven's interactive
`addhomelabkeys` (see the "Riven - 1Password SSH Loader" Obsidian guide —
its "LLM" vault name is itself stale documentation; the real vault for both
hosts is `Homelab`).

If `git pull` ever fails again on `arc`, check the agent before assuming a
GitHub-side problem:

```bash
ssh arc "SSH_AUTH_SOCK=/root/.ssh/agent.sock ssh-add -l"
ssh arc "systemctl status homelab-ssh-agent.service homelab-op-ssh-load.timer --no-pager"
ssh arc "journalctl -u homelab-op-ssh-load.service -n 20 --no-pager"
ssh arc "/root/.local/bin/addhomelabkeys"   # force an immediate reload
```

## Configuration Files

**On `arc`:**
- `/usr/local/sbin/homelab-postinstall-webhook` — `:9443` HTTP listener (manual/alternate trigger path)
- `/usr/local/sbin/homelab-pdm-installation-watch` — PDM API poller (primary trigger path)
- `/usr/local/sbin/homelab-pdm-refresh-remote` — refreshes a standalone PVE host's PDM remote trust/token post-deploy
- `/usr/local/sbin/homelab-postinstall-deploy` — waits for SSH, git pulls, runs `./deploy all <host>`
- `/root/.local/bin/op-ssh-add` — 1Password-backed single-key loader (`main`|`pve`)
- `/root/.local/bin/addhomelabkeys` — loads both keys into `/root/.ssh/agent.sock`
- `/root/.config/op-ssh-agent.env` — item refs, TTLs, offsite forbidden-item guard
- `/etc/systemd/system/homelab-postinstall-webhook.service`
- `/etc/systemd/system/homelab-pdm-installation-watch.{service,timer}` (60s poll)
- `/etc/systemd/system/homelab-ssh-agent.service` — persistent root `ssh-agent -a /root/.ssh/agent.sock`
- `/etc/systemd/system/homelab-op-ssh-load.{service,timer}` — reloads both keys (`OnBootSec=10s`, `OnUnitActiveSec=7h`)
- `/etc/homelab-postinstall-webhook/env` — `WEBHOOK_TOKEN`, `PDM_TOKEN_ID`/`PDM_TOKEN_SECRET`, `REPO_DIR`, `DRY_RUN`, timeouts
- `/var/lib/homelab-postinstall-webhook/state/<uuid>.queued` — dedup marker, one per queued install
- `/var/lib/homelab-postinstall-webhook/events/*.json` — raw install/webhook payloads for each queued deploy

**In this repo:**
- `scripts/homelab-postinstall-webhook.py`
- `scripts/homelab-pdm-installation-watch.py`
- `scripts/homelab-pdm-refresh-remote.py`
- `scripts/homelab-postinstall-deploy.sh`
- `scripts/op-ssh-add`
- `scripts/addhomelabkeys`
- `scripts/op-ssh-agent.conf` (deploys as `/root/.config/op-ssh-agent.env` on `arc` — named `.conf` here only because `.gitignore` blanket-excludes `**/*.env`; carries no secrets)
- `scripts/homelab-ssh-agent.service`
- `scripts/homelab-op-ssh-load.{service,timer}`
- `scripts/install.sh`

See also the `pve-autoinstall` module, which owns the prepared-answer content
(including `post-hook-base-url`) that this module's flow depends on.

## Deployment

```bash
./deploy pve-postinstall-webhook arc
```

## Verification

```bash
ssh arc "systemctl status homelab-postinstall-webhook.service --no-pager"
ssh arc "systemctl list-timers homelab-pdm-installation-watch.timer --no-pager"
ssh arc "journalctl -u homelab-pdm-installation-watch.service -n 20 --no-pager"
```
