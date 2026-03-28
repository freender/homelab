# SSH Config Auto-Deploy

Automated SSH config deployment across homelab infrastructure.

## Deployment

Deploy to all hosts:
```bash
cd ~/homelab/ssh && ./deploy.sh all
```

Deploy to specific hosts:
```bash
cd ~/homelab/ssh && ./deploy.sh ace bray clovis
```

Deploy to single host:
```bash
cd ~/homelab/ssh && ./deploy.sh tower
```

## Configuration

SSH config uses internal DNS naming:
- Home network: `*.freender.internal`
- Remote sites: `cottonwood.internal`, `cinci.internal`

Current special aliases:
- `orbit` -> `orbit.freender.internal` (root, key-only)
- `zavala` -> `192.168.86.77:2222` (root, key-only)

## Features

- **Auto-accept host keys:** Uses `StrictHostKeyChecking=accept-new`
- **Scoped agent forwarding:** Disabled by default, enabled only for `riven`
- **Host-specific configs:** Special handling for hosts with custom requirements
- **DNS-based:** All hosts use internal DNS instead of IPs

## Troubleshooting

- If `zavala` asks for password even with key present, fix key ownership inside the Incus container:

```bash
ssh cinci 'sudo incus exec zavala -- chown root:root /root/.ssh/authorized_keys && sudo incus exec zavala -- chmod 600 /root/.ssh/authorized_keys'
```

## Structure

```
ssh_config           # Main SSH configuration
deploy.sh           # Deployment script
```
