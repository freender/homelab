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

- `/usr/share/perl5/PVE/LXC/Migrate.pm` — snapshot ordering fix.
  Moves `run_replication()` to after `umount_all()` + `deactivate_volumes()`
  so the ZFS dataset is unmounted before the migration snapshot is taken.
  Unmounting is what flushes kernel page-cache dirty pages (including bbolt
  `mmap` writes) to the ZFS vnode.  `zpool sync` alone is not sufficient:
  pages dirty in the page cache but not yet written down to ZFS are invisible
  to `zpool sync`.  Previously `run_replication()` ran before `umount_all()`,
  so the snapshot captured stale page-cache state rather than the fully
  flushed post-stop file contents.

Without all three patches the snapshot can diverge from the live mounted view
and send stale blocks to the target node, causing database corruption (e.g.
bbolt `meta.db` with the same mtime and size but different sha256 between
source live dataset and migration snapshot, leading to a containerd panic on
destination start).

Root cause chain:
1. CT stops — containerd flushes bbolt writes via `mmap` to the page cache.
2. `run_replication()` fires (old: before unmount) — `zpool sync` runs but
   page-cache pages haven't reached ZFS yet; snapshot captures stale data.
3. `zfs send` ships the snapshot — destination receives stale `meta.db`.
4. Containerd on destination opens the corrupt bbolt database and panics.

Fix: unmount first (page-cache flush → ZFS), then `zpool sync` (ZFS → disk),
then snapshot.

Upstream tracking: Bug 7653 - LXC migration on zfspool snapshot may contain
stale data: https://bugzilla.proxmox.com/show_bug.cgi?id=7653

Remove this module and revert the local patches when Proxmox ships equivalent
upstream behavior for this bug.

Deployment also installs `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
and an apt `DPkg::Post-Invoke` hook so `libpve-storage-perl` and `pve-container`
package upgrades that replace any of the three patched files are repatched
automatically. The patch script restarts `pvescheduler`, `pvedaemon`, `pvestatd`,
and `pve-ha-lrm` after patch verification so long-lived Proxmox Perl daemons
reload all three modules before the next migration.

Operational files:

- Script: `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-zfs-migration-sync-patch`
- Status: `/var/lib/homelab/pve-zfs-migration-sync-patch/status`
- Backups: `/var/backups/homelab/pve-zfs-migration-sync-patch/`
- Reloaded services: `pvescheduler`, `pvedaemon`, `pvestatd`, `pve-ha-lrm`
