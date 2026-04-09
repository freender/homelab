# SSH Config Auto-Deploy

Automated SSH config deployment across homelab infrastructure.

## Deployment

Deploy to all hosts:
```bash
cd ~/homelab && ./deploy ssh-config all
```

Deploy to specific hosts:
```bash
cd ~/homelab && ./deploy ssh-config riven
```

Deploy to single host:
```bash
cd ~/homelab && ./deploy ssh-config exo
```

## Configuration

Global SSH defaults live in `ssh-config/configs/common.conf`.

Per-host connection metadata lives in `hosts.conf` under `config`:
- `config.hostname`
- `config.user`
- `config.sshkey`
- `config.agent` (only where needed, like `exo`)

Optional generated SSH overrides also live under `config`:
- `config.ssh_config.user`
- `config.ssh_config.sshkey`

When those overrides differ from the canonical deploy connection, the generator keeps the base host name for the interactive default and also emits a `<host>-root` alias for the canonical deploy/root path.

Hosts currently managed by this module:
- `riven`
- `exo`

## Features

- **Auto-accept host keys:** Uses `StrictHostKeyChecking=accept-new`
- **Connection keepalive:** Uses server alive probes to survive idle sessions
- **Known host privacy:** Uses `HashKnownHosts=yes`
- **Inventory-driven identities:** Uses `hosts.conf` `config.sshkey` metadata to assign `homelab` vs `infra`
- **Agent-aware paths:** `exo` uses `config.agent: op` and `.pub` identity stubs; other hosts use standard key paths
- **DNS-based:** All hosts use internal DNS instead of IPs

## Troubleshooting

- If `zavala` asks for password even with key present, fix key ownership inside the Incus container:

```bash
ssh cinci 'sudo incus exec zavala -- chown root:root /root/.ssh/authorized_keys && sudo incus exec zavala -- chmod 600 /root/.ssh/authorized_keys'
```

## Structure

```
configs/common.conf         # Shared Host * defaults
scripts/install.sh          # Remote installer
../deploy                   # Repo-root deployment wrapper
```
