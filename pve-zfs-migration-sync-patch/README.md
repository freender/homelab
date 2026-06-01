# PVE ZFS Migration Sync Patch

Adds a `zpool sync <pool>` call before Proxmox creates migration snapshots for
ZFS-backed storage, covering both code paths that take snapshots during
HA-managed CT migration.

**Patched files:**

- `/usr/share/perl5/PVE/Storage.pm` — non-replicated migration path.
  Fixes `$volume_export_prepare` so `storage_migrate()` flushes dirty TXG
  data before taking the `__migration__` snapshot.

- `/usr/share/perl5/PVE/Replication.pm` — replicated migration path.
  Fixes `replicate()` so `run_replication()` during HA migrate flushes dirty
  TXG data before taking the `__replicate_*` snapshot.  Without this fix the
  `Storage.pm` change has no effect for CTs with a PVE replication job:
  `LXC/Migrate.pm` calls `run_replication()` first, marks those volumes in
  `$rep_volumes`, and then skips `storage_migrate()` entirely for them
  (`next if $rep_volumes->{$volid}`), so the `Storage.pm` path is never
  reached.

Without either sync the snapshot can diverge from the live mounted view and
send stale blocks to the target node, causing database corruption (e.g. bbolt
`meta.db` sha256 mismatch between source live dataset and migration snapshot,
leading to a containerd panic on destination start).

Upstream tracking: Bug 7653 - LXC migration on zfspool snapshot may contain
stale data: https://bugzilla.proxmox.com/show_bug.cgi?id=7653

Remove this module and revert the local patches when Proxmox ships equivalent
upstream behavior for this bug.

Deployment also installs `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
and an apt `DPkg::Post-Invoke` hook so `libpve-storage-perl` and `pve-container`
package upgrades that replace `Storage.pm` or `Replication.pm` are patched again
automatically. The patch script restarts `pvescheduler`, `pvedaemon`, `pvestatd`,
and `pve-ha-lrm` after patch verification so long-lived Proxmox Perl daemons
reload both modules before scheduled replication or migration uses the code path.

Operational files:

- Script: `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-zfs-migration-sync-patch`
- Status: `/var/lib/homelab/pve-zfs-migration-sync-patch/status`
- Backups: `/var/backups/homelab/pve-zfs-migration-sync-patch/`
- Reloaded services: `pvescheduler`, `pvedaemon`, `pvestatd`, `pve-ha-lrm`
