# PVE ZFS Migration Sync Patch

Adds a `zpool sync <pool>` call before Proxmox creates migration snapshots for
ZFS-backed storage, covering both code paths that take snapshots during
HA-managed CT migration.

**Patched files:**

- `/usr/share/perl5/PVE/Storage.pm` — non-replicated migration path.
  Fixes `$volume_export_prepare` so `storage_migrate()` flushes dirty TXG
  data before taking the `__migration__` snapshot.

- `/usr/share/perl5/PVE/Replication.pm` — replicated migration path.
  Fixes `replicate()` so `run_replication()` during HA migrate flushes both
  the kernel page cache and ZFS dirty TXGs before taking the `__replicate_*`
  snapshot.  Without this fix the `Storage.pm` change has no effect for CTs
  with a PVE replication job: `LXC/Migrate.pm` calls `run_replication()`
  first, marks those volumes in `$rep_volumes`, and then skips
  `storage_migrate()` entirely (`next if $rep_volumes->{$volid}`), so the
  `Storage.pm` path is never reached.

  The fix applies two operations per ZFS volume before snapshotting:

  1. `syncfs()` via `/usr/bin/sync --file-system <mountpoint>` — flushes all
     kernel page-cache dirty pages to the ZFS vnode. This is the critical
     step: mmap writers (bbolt, sqlite WAL, any app using lazy msync) leave
     pages dirty in the page cache after guest shutdown; ZFS cannot see these
     pages until they are pushed down to the VFS layer. `zpool sync` alone
     cannot reach them.

  2. `zpool sync <pool>` — flushes ZFS dirty TXGs to disk so the snapshot
     captures the fully committed post-stop state.

  Complete flush chain: page cache → ZFS vnode → disk → `zfs snapshot`.
  This is a general fix requiring no guest-specific hooks or knowledge.

Root cause chain:
1. CT stops — guest writes via `mmap` flush to the kernel page cache only.
2. `run_replication()` fires (dataset still mounted, page cache not flushed).
3. Without `syncfs`: page-cache dirty pages are invisible to ZFS; `zpool sync`
   has nothing to flush for them; snapshot captures stale data.
4. `zfs send` ships the stale snapshot — destination receives corrupt db.
5. Containerd opens the corrupt bbolt database and panics.

Upstream tracking: Bug 7653 - LXC migration on zfspool snapshot may contain
stale data: https://bugzilla.proxmox.com/show_bug.cgi?id=7653

Remove this module and revert the local patches when Proxmox ships equivalent
upstream behavior for this bug.

Deployment also installs `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
and an apt `DPkg::Post-Invoke` hook so `libpve-storage-perl` and `pve-container`
package upgrades that replace `Storage.pm` or `Replication.pm` are repatched
automatically. The patch script restarts `pvescheduler`, `pvedaemon`, `pvestatd`,
and `pve-ha-lrm` after patch verification so long-lived Proxmox Perl daemons
reload both modules before the next migration.

Operational files:

- Script: `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-zfs-migration-sync-patch`
- Status: `/var/lib/homelab/pve-zfs-migration-sync-patch/status`
- Backups: `/var/backups/homelab/pve-zfs-migration-sync-patch/`
- Reloaded services: `pvescheduler`, `pvedaemon`, `pvestatd`, `pve-ha-lrm`
