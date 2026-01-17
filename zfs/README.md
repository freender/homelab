# homelab/zfs

ZFS automation for cottonwood, cinci, and tower:
- ZED Telegram notifications for ZFS errors (cottonwood/cinci)
- Shared snapshot and appdata replication scripts (cottonwood/cinci)
- Tower-specific ZFS/backup scripts (tower)
- Cron jobs logging to `/mnt/cache/appdata/scripts/logs` (cottonwood/cinci)

## Repo Layout
\`\`\`
.
├── .env.example
├── deploy.sh
├── scripts/
│   ├── zfs_snapshots.sh               # cottonwood/cinci
│   ├── zfs_replication_appdata.sh     # cottonwood/cinci
│   ├── zfs_replication_cache.sh       # tower
│   ├── zfs_timemachine_cleanup.sh     # tower
│   ├── rsync_flash.sh                 # tower
│   └── rsync_pi_kvm_backup.sh         # tower
└── zed-telegram-notify.sh             # cottonwood/cinci
\`\`\`

## Setup

### For cottonwood/cinci
1. Copy credentials file:
   \`\`\`bash
   cp .env.example .env
   \`\`\`
2. Edit \`.env\` with Telegram credentials:
   \`\`\`bash
   TELEGRAM_TOKEN=...
   TELEGRAM_CHAT_ID=...
   \`\`\`

### For tower
No setup required - scripts deploy directly.

## Deploy
\`\`\`bash
./deploy.sh all            # Deploy to all hosts
./deploy.sh tower          # Tower only
./deploy.sh cottonwood     # Cottonwood only
./deploy.sh cinci          # Cincinnati only
\`\`\`

## What Gets Deployed

### cottonwood/cinci
- ZED notifier to \`/etc/zfs/zed.d/telegram-notify.sh\`
- ZFS scripts to `/mnt/cache/appdata/scripts`
- Cron entries:
  - `0 0 * * * sudo /mnt/cache/appdata/scripts/zfs_snapshots.sh >> /mnt/cache/appdata/scripts/logs/zfs_snapshots.log 2>&1`
  - `10 0 * * * sudo /mnt/cache/appdata/scripts/zfs_replication_appdata.sh >> /mnt/cache/appdata/scripts/logs/zfs_replication_appdata.log 2>&1`

### tower
- ZFS/backup scripts to \`/mnt/cache/appdata/scripts/\`
  - \`zfs_replication_cache.sh\` - Replicates ZFS cache pool datasets
  - \`zfs_timemachine_cleanup.sh\` - Cleans old Time Machine snapshots
  - \`rsync_flash.sh\` - Backs up Unraid USB boot drive
  - \`rsync_pi_kvm_backup.sh\` - Backs up Pi-KVM configs
- Scheduling handled by User Scripts plugin (see homelab/filebot)

## Test ZED (cottonwood/cinci)
\`\`\`bash
ssh <host> "sudo ZEVENT_POOL=cache ZEVENT_SUBCLASS=statechange ZEVENT_VDEV_STATE_STR=DEGRADED /etc/zfs/zed.d/telegram-notify.sh"
\`\`\`

## Script Details

### cottonwood/cinci Scripts
- \`zfs_snapshots.sh\` - Excludes \`appdata\`, \`backup/appdata\`, and \`pbs-datastore\`
- \`zfs_replication_appdata.sh\` - Uses ZFS replication (syncoid) to \`cache/backup/appdata\`
- Notifications use \`/etc/zfs/zed.d/.env\` (same as ZED alerts)

### tower Scripts
- \`zfs_replication_cache.sh\` - Full ZFS snapshot/replication script with auto-dataset selection
- \`zfs_timemachine_cleanup.sh\` - Keeps only most recent Time Machine snapshot
- \`rsync_flash.sh\` - Backs up \`/boot\` to \`/mnt/cache/tower/boot\`
- \`rsync_pi_kvm_backup.sh\` - Backs up Pi-KVM configs to \`/mnt/cache/tower/pi-kvm\`
