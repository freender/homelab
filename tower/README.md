# homelab/tower

Tower (Unraid) specific scripts for maintenance and monitoring.

## Scripts

### filebot_monitor.sh
Monitors FileBot download folder for stuck imports.

**What it does:**
- Watches \`.mkv\` and \`.mp4\` files in \`/mnt/user/media/downloads/filebot\`
- Tracks files older than 5 minutes
- Sends Unraid notification if file not imported after 15 minutes
- Maintains state file to avoid duplicate alerts
- Auto-purges old tracking data after 30 days

**Configuration:**
- \`WORK_DIR\` - FileBot folder location
- \`ALERT_AGE_MINUTES\` - Minimum age before tracking (default: 15)
- \`PURGE_DAYS\` - Days to keep alerted entries (default: 30)

**User Scripts setup:**
- Name: \`filebot_monitor\`
- Schedule: \`*/5 * * * *\` (every 5 minutes)
- Script: \`#!/bin/bash\n/mnt/cache/appdata/scripts/filebot_monitor.sh\`

## Deployment

**Source:** https://github.com/freender/homelab

Deploy from helm:

\`\`\`bash
cd ~/homelab/tower
./deploy.sh
\`\`\`

Deploys to: \`/mnt/cache/appdata/scripts/\`

## User Scripts Plugin

Scripts are called via thin wrappers in User Scripts plugin on boot device:

\`\`\`
/boot/config/plugins/user.scripts/scripts/
├── docker_compose_update/script  → /mnt/cache/appdata/start.sh
├── filebot_monitor/script         → /mnt/cache/appdata/scripts/filebot_monitor.sh
├── zfs_replication_cache/script   → /mnt/cache/appdata/scripts/zfs_replication_cache.sh
├── zfs_timemachine_cleanup/script → /mnt/cache/appdata/scripts/zfs_timemachine_cleanup.sh
├── rsync_flash/script             → /mnt/cache/appdata/scripts/rsync_flash.sh
└── rsync_pi_kvm_backup/script     → /mnt/cache/appdata/scripts/rsync_pi_kvm_backup.sh
\`\`\`

**Why thin wrappers?**
- Real scripts live in git-tracked \`/mnt/cache/appdata/scripts/\`
- User Scripts plugin handles scheduling from boot device
- Easy to update/restore scripts from git
- Boot device only contains 2-line wrappers

## Related

- Docker scripts: \`~/homelab/docker\`
- ZFS scripts: \`~/homelab/zfs\`
