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

## Features

- **Auto-accept host keys:** Uses `StrictHostKeyChecking=accept-new`
- **Host-specific configs:** Special handling for hosts with custom requirements
- **DNS-based:** All hosts use internal DNS instead of IPs

## Structure

```
ssh_config           # Main SSH configuration
deploy.sh           # Deployment script
```
