# homelab/pve-notifications

Deploys Proxmox notification targets and matchers to PVE hosts.

## What It Manages
- `/etc/pve/notifications.cfg`
- `/etc/pve/priv/notifications.cfg`

## Secrets

Create `configs/telegram.env` from the example file:

```bash
cd ~/homelab/pve-notifications
cp configs/telegram.env.example configs/telegram.env
```

Set:
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHATID`

`configs/telegram.env` is gitignored.

If `configs/telegram.env` is missing, deploy also supports fallback to
`apcupsd/configs/telegram/telegram.env`.

## Deployment

Deploy to all enabled hosts:

```bash
cd ~/homelab/pve-notifications
./deploy.sh all
```

Deploy to one host:

```bash
./deploy.sh osiris
```
