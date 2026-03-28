# SSH Config Auto-Deploy

Automated SSH config deployment across homelab infrastructure.

## Deployment

Deploy to all hosts:
```bash
cd ~/homelab/ssh && ./deploy.sh all
```

Deploy to specific hosts:
```bash
cd ~/homelab/ssh && ./deploy.sh helm
```

Deploy to single host:
```bash
cd ~/homelab/ssh && ./deploy.sh exo
```

## Configuration

SSH config uses internal DNS naming:
- Home network: `*.freender.internal`
- Remote sites: `cottonwood.internal`, `cinci.internal`

Current special aliases:
- `orbit` -> `orbit.freender.internal` (root, key-only)

Hosts currently managed by this module:
- `helm`
- `riven`
- `exo`

## Features

- **Auto-accept host keys:** Uses `StrictHostKeyChecking=accept-new`
- **Connection keepalive:** Uses server alive probes to survive idle sessions
- **Connection reuse:** Uses SSH multiplexing via `~/.ssh/sockets/`
- **Known host privacy:** Uses `HashKnownHosts=yes`
- **Scoped agent forwarding:** Disabled by default, enabled only for `riven`
- **Host-specific configs:** Special handling for hosts with custom requirements
- **DNS-based:** All hosts use internal DNS instead of IPs

## Troubleshooting

- If a host-specific key path only exists on one machine, keep it in that host's append config instead of `configs/common.conf`.

- If `zavala` asks for password even with key present, fix key ownership inside the Incus container:

```bash
ssh cinci 'sudo incus exec zavala -- chown root:root /root/.ssh/authorized_keys && sudo incus exec zavala -- chmod 600 /root/.ssh/authorized_keys'
```

## Structure

```
configs/common.conf         # Shared SSH config
configs/<host>/append.conf  # Host-specific overrides
scripts/install.sh          # Remote installer
deploy.sh                   # Deployment script
```
