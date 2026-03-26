# SSH Config Auto-Deploy

Automated SSH config deployment across homelab infrastructure.

Targets are driven by `ssh` entries in `hosts.conf`.

## Deployment

Deploy to all hosts:
```bash
cd ~/homelab/ssh && ./deploy.sh all
```

Deploy to specific hosts:
```bash
cd ~/homelab/ssh && ./deploy.sh helm orbit
```

Deploy to single host:
```bash
cd ~/homelab/ssh && ./deploy.sh orbit
```

## Configuration

SSH config uses internal DNS naming:
- Home network: `*.freender.internal`
- Remote sites: `cottonwood.internal`, `cinci.internal`

Current special aliases:
- `orbit` -> `orbit.freender.internal` (root, key-only)

## Features

- **Auto-accept host keys:** Uses `StrictHostKeyChecking=accept-new`
- **Host-specific configs:** Special handling for hosts with custom requirements
- **DNS-based:** All hosts use internal DNS instead of IPs
- **Dedicated traefik-sync bundle:** Hosts with `ssh.traefik_sync: true` get `~/traefik-sync/.ssh/` with a dedicated key, `known_hosts`, and test config

## Current Targets

- `exo` - workstation SSH config
- `helm` - user SSH config + dedicated `traefik-sync` SSH bundle
- `orbit` - root SSH config + dedicated `traefik-sync` SSH bundle

## Traefik Sync

For hosts with `ssh.traefik_sync: true`, deploy also manages:

- `~/traefik-sync/.ssh/config`
- `~/traefik-sync/.ssh/id_ed25519`
- `~/traefik-sync/.ssh/id_ed25519.pub`
- `~/traefik-sync/.ssh/known_hosts`

Notes:

- The key is generated once if missing and preserved on later deploys.
- `known_hosts` is refreshed from `tower.freender.internal` during install.
- `traefik-sync` currently runs `ssh -F /dev/null`, so mount `~/traefik-sync/.ssh` into the container as `/root/.ssh`.
- Add the generated public keys from `helm` and `orbit` to `tower` user `freender` `authorized_keys`.

Example mount:

```yaml
volumes:
  - /home/freender/traefik-sync/.ssh:/root/.ssh:ro
```

Manual connectivity test on the host:

```bash
ssh -F ~/traefik-sync/.ssh/config tower-traefik-sync true
```

## Troubleshooting

- If `zavala` asks for password even with key present, fix key ownership inside the Incus container:

```bash
ssh cinci 'sudo incus exec zavala -- chown root:root /root/.ssh/authorized_keys && sudo incus exec zavala -- chmod 600 /root/.ssh/authorized_keys'
```

## Structure

```
configs/common.conf       # Main SSH configuration
configs/traefik-sync.conf # Dedicated traefik-sync test config
deploy.sh                 # Deployment script
```
