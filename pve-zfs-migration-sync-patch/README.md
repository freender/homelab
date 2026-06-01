# PVE ZFS Migration Sync Patch

Adds a `zpool sync <pool>` call before Proxmox creates migration snapshots for
ZFS-backed storage, covering both code paths that take snapshots during
HA-managed CT migration.

**Patched files:**

- `/usr/share/perl5/PVE/Storage.pm` — non-replicated migration path.
  Fixes `$volume_export_prepare` so `storage_migrate()` flushes dirty TXG
  data before taking the `__migration__` snapshot.

- `/usr/share/perl5/PVE/Replication.pm` — replicated migration path.
  Fixes `replicate()` so `run_replication()` during HA migrate also flushes
  dirty TXG data before taking the `__replicate_*` snapshot.  Without this
  fix the `Storage.pm` change has no effect for CTs with a PVE replication
  job: `LXC/Migrate.pm` calls `run_replication()` first, marks those volumes
  in `$rep_volumes`, and then skips `storage_migrate()` entirely for them
  (`next if $rep_volumes->{$volid}`), so the `Storage.pm` path is never
  reached.

**Known limitation — kernel page-cache ordering (requires upstream fix):**

`zpool sync` flushes data ZFS already knows about, but cannot flush kernel
page-cache dirty pages that have not yet been written down to the ZFS vnode.
bbolt uses `mmap` for writes; after CT stop these pages may be dirty in the
page cache but invisible to ZFS. The snapshot can therefore still capture
stale data.

The correct fix is to move `run_replication()` in `LXC/Migrate.pm` to after
`umount_all()` + `deactivate_volumes()` — unmounting guarantees all
page-cache pages are flushed to ZFS before the snapshot is taken. A local
patch to `Migrate.pm` was tested but caused volume renaming corruption
(`allow_rename => 1` in the non-replicated storage_migrate loop ran against
volumes that should have been skipped by `$rep_volumes`) and was reverted.
The correct fix requires restructuring `phase1` in `pve-container` to keep
the `$rep_volumes` guard intact when `run_replication()` is deferred — this
should be submitted upstream.

Root cause chain:
1. CT stops — containerd flushes bbolt writes via `mmap` to the page cache.
2. `run_replication()` fires before `umount_all()` — `zpool sync` runs but
   page-cache pages haven't reached ZFS yet; snapshot captures stale data.
3. `zfs send` ships the snapshot — destination receives stale `meta.db`.
4. Containerd on destination opens the corrupt bbolt database and panics.

Complete fix sequence: CT stop → `umount_all()` (page-cache → ZFS) →
`zpool sync` (ZFS → disk) → `zfs snapshot` → `zfs send`.

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
