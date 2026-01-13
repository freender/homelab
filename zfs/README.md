# homelab/zfs

ZFS automation for cottonwood and cinci:
- ZED Telegram notifications for ZFS errors
- Shared snapshot and appdata replication scripts
- Cron jobs logging to `$HOME/zfs-logs`

## Repo Layout
```
.
├── .env.example
├── deploy.sh
├── scripts/
│   ├── zfs_snapshots.sh
│   └── zfs_replication_appdata.sh
└── zed-telegram-notify.sh
```

## Setup
1. Copy credentials file:
   ```bash
   cp .env.example .env
   ```
2. Edit `.env` with Telegram credentials:
   ```bash
   TELEGRAM_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```

## Deploy
```bash
./deploy.sh all
```

Deploys:
- ZED notifier to `/etc/zfs/zed.d/telegram-notify.sh`
- ZFS scripts to `$HOME/zfs-scripts`
- Cron entries:
  - `00 0 * * * sudo $HOME/zfs-scripts/zfs_snapshots.sh >> $HOME/zfs-logs/zfs_snapshots.log 2>&1`
  - `10 0 * * * sudo $HOME/zfs-scripts/zfs_replication_appdata.sh >> $HOME/zfs-logs/zfs_replication_appdata.log 2>&1`

## Test
```bash
ssh <host> "sudo ZEVENT_POOL=cache ZEVENT_SUBCLASS=statechange ZEVENT_VDEV_STATE_STR=DEGRADED /etc/zfs/zed.d/telegram-notify.sh"
```

## Notes
- `zfs_snapshots.sh` excludes `appdata`, `backup/appdata`, and `pbs-datastore`.
- `zfs_replication_appdata.sh` uses ZFS replication (syncoid) to `cache/backup/appdata`.
- Script notifications use `/etc/zfs/zed.d/.env` (same as ZED alerts).
