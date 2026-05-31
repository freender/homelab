# PVE ZFS Migration Sync Patch

Adds a `zpool sync <pool>` call before Proxmox creates migration snapshots for
ZFS-backed storage in `/usr/share/perl5/PVE/Storage.pm`.

This patches the migration snapshot path so `storage_migrate()` does not take a
ZFS snapshot while recent guest writes are still only present in the current
dirty TXG. Without the sync, the snapshot can diverge from the live mounted
view and send stale blocks to the target node.

Remove this module when Proxmox ships equivalent upstream behavior.

Deployment also installs `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
and an apt `DPkg::Post-Invoke` hook so `libpve-storage-perl` package upgrades
 that replace `Storage.pm` are patched again automatically.

Operational files:

- Script: `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-zfs-migration-sync-patch`
- Status: `/var/lib/homelab/pve-zfs-migration-sync-patch/status`
- Backups: `/var/backups/homelab/pve-zfs-migration-sync-patch/`
