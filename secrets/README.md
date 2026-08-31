# Secrets

Homelab repo deploy-time secrets are sourced from 1Password via `op inject`.
Nothing sensitive is committed to this repo or written to persistent disk on
`riven`. Rendered secret files exist only in `/dev/shm` for the lifetime of a
deploy and are shredded on exit.

Compose `.env` files on the Docker hosts (`tower`/`helm`/`neo`) are out of
scope for this layer.

## Layout

| Path | Purpose | Committed? |
|------|---------|------------|
| `catalog.yml` | Maps stable names to op-inject templates | yes |
| `templates/<name>.env.tpl` | `op://` references rendered at deploy time | yes |
| `templates/<name>.env.tpl.example` | Offline-mode placeholder (used by `homelab validate`) | yes |
| `*.env` | Legacy plaintext secrets, being phased out | no (gitignored) |

## Authentication on `riven`

A 1Password service-account token lives at `~/.config/op/homelab.token` or the
existing OpenCode token path `~/.config/op/service-account-token`, with mode
`0600`. The deploy CLI reads it once per process, exports it as
`OP_SERVICE_ACCOUNT_TOKEN` for child `op` invocations, and never echoes it.

```bash
install -m 0600 /dev/null ~/.config/op/homelab.token
$EDITOR ~/.config/op/homelab.token   # paste token, save
chmod 600 ~/.config/op/homelab.token
```

The CLI prefers `homelab.token` if present, otherwise reuses
`service-account-token`. It refuses to use the token if the file is group/world
readable or owned by another user.

## 1Password vault layout

Vault: `Homelab`. Each item is referenced by name in the templates.

| Item | Required fields |
|------|-----------------|
| `PBS Backup Main` | `password`, `fingerprint` |
| `PBS Backup Cinci` | `password`, `fingerprint` |
| `PBS Backup Cinci Hosts` | `password`, `fingerprint` |
| `PBS Backup Xur Cinci` | `password`, `token_secret`, `fingerprint` |
| `PBS Backup Xur Cottonwood` | `fingerprint`, `token_secret` |
| `Telegram Homelab Bot` | `token`, `chat_id` |
| `Keepalived Healthchecks` | `helm_host`, `helm_url`, `neo_host`, `neo_url`, `tower_host`, `tower_url` |
| `Network MACs` | `cinci_primary`, `cottonwood_primary` |

Field names are lowercase-with-underscores so they survive label renames in
the 1Password UI.

## Workflow

```bash
# Verify every catalog entry resolves without printing values.
homelab secrets doctor

# List all configured secret names.
homelab secrets list

# Render every secret into /dev/shm for manual inspection.
# The directory is shredded when this process exits.
homelab secrets render
```

The service account needs only **read** access on the `Homelab` vault. Deploys
and `homelab secrets doctor` never write.

Items are created and edited in the 1Password UI. There is no repo-side write
path: the one-time `secrets bootstrap` / `secrets purge-local` migration from
legacy plaintext `secrets/*.env` files completed, no such files remain, and the
commands were removed rather than left as a rw-capable code path nothing uses.
See git history if the migration itself is ever of interest.

## Adding a new secret

1. Create the item in the `Homelab` 1Password vault with the field names you
   want to reference.
2. Create `secrets/templates/<name>.env.tpl` with `{{ op://Homelab/<Item>/<field> }}`
   placeholders. Match the env file shape the consumer expects.
3. Create `secrets/templates/<name>.env.tpl.example` with placeholder values
   so offline validation (`homelab validate`) keeps working.
4. Add an entry to `secrets/catalog.yml`.
5. In the consuming module, import `op_secrets` and call
   `op_secrets.secret_file(root, "<name>")`. The returned path lives in
   `/dev/shm` and is auto-cleaned.
6. Run `homelab secrets doctor` to verify resolution.

## Offline mode

`HOMELAB_OFFLINE=1` (set by `homelab validate`) bypasses `op` entirely. Modules
get the `.env.tpl.example` file instead. This is why every template must ship
an example sibling, even though the example values are placeholders.

## Why no env files on `riven` disk?

- The repo is the deploy controller, not a credential store.
- `op` is the source of truth and gives you per-item access logging.
- Rendered files live only in `/dev/shm` (RAM-backed tmpfs) so they vanish
  on reboot and are never written to a persistent block device.
- A leaked working copy of the repo is not also a leaked secret.

## What about the Docker compose `.env` files?

Those live on the Docker hosts under `/mnt/cache/appdata/<app>/`, are
backed up via PBS, replicated, and read by containers at start time.
Pulling them from 1Password at container runtime would add fragility for
no real benefit; they are intentionally out of scope for this layer.
