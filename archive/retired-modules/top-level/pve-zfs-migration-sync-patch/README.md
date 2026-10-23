# PVE ZFS Migration Receive Cache Patch

> **Retired 2026-10-23.** Proxmox fixed bug 7653 in `proxmox-kernel-7.0.14-26-pve`
> (backport of `openzfs/zfs#19013`). The patch was reverted on ace, bray and
> clovis before they booted -26. Once all three ran -26, the mmap reproducer was
> clean in both rollback and recv modes on every node. 16 real stopped
> migrations were then clean too, with no patch in place: tower ace<->clovis x4
> and neo bray<->ace x4, each received into a warm, mounted target. Live BoltDB
> files matched the `__replicate_*` snapshot every time.

Patches Proxmox ZFS storage import so LXC `subvol` datasets are unmounted after
`zfs recv -F`. This mitigates stale Linux page-cache/live-mount reads after PVE
ZFS replication or migration receives into an already-mounted target dataset.

## Patched File

- `/usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm`
  - Function: `PVE::Storage::ZFSPoolPlugin::volume_import`
  - Adds target-side `zfs unmount <dataset>` after successful receive, so normal
    PVE activation or CT start remounts a fresh live view.

## Why

Observed failure mode during PVE stopped LXC migration on ZFS:

1. PVE receives an incremental stream into a mounted target CT rootfs dataset.
2. The target `__replicate_*` snapshot contains the expected bytes.
3. The target live mounted path can still serve stale page-cache contents for
   `/var/lib/containerd/io.containerd.metadata.v1.bolt/meta.db`.
4. File metadata can match exactly and `zfs diff` can report `0` lines.
5. Starting the CT in that state can make containerd/BoltDB panic.

Tested mitigations:

- `echo 3 > /proc/sys/vm/drop_caches` fixed the stale live read.
- `zfs unmount && zfs mount` fixed the stale live read.
- `sync` alone did not fix it.
- Receiving into an unmounted target dataset prevented the mismatch in testing.
- Later CT 902 testing showed both before-only and after-only unmount variants
  were sufficient; after-only was kept because it directly guarantees a fresh
  mount for PVE activation/start and avoids changing the receive precondition.

This patch uses the least global mitigation: leave the received `subvol`
unmounted for PVE activation/start to remount.

## Superseded Patch (Historical)

This module previously patched `Storage.pm` and `Replication.pm` to call
`syncfs()` before migration snapshots. Later testing showed the problem is not
source-side snapshot flushing. It is target-side stale live reads after receive.

All deploy targets (ace, bray, clovis) were migrated off that approach and the
old reapply script/apt hook (`homelab-pve-zfs-migration-sync-patch`) were
removed from those hosts. The one-time cleanup code that performed this
migration has since been dropped from `install.sh` since it is a permanent
no-op on every current target; a fresh host would never have the old
artifacts to clean up either.

## Upstream Tracking

Proxmox Bugzilla:

- Bug 7653 - LXC migration on zfspool snapshot may contain stale data
- https://bugzilla.proxmox.com/show_bug.cgi?id=7653

The current evidence likely also belongs upstream to OpenZFS/Linux because the
observable mismatch is live mounted file contents versus the same dataset's
snapshot contents after `zfs receive`.

### Recurrence: 2026-10-12 (patch lapse)

Third containerd BoltDB corruption on tower (CT 101), after earlier ones on
2026-05-10 and 2026-05-12. It happened while the patch was missing, and stopped
once the patch was back:

1. 2026-10-10: the weekly apt run upgraded `libpve-storage-perl` (9.1.11 to
   9.1.12) and replaced `ZFSPoolPlugin.pm` on ace and clovis. The
   large-block patch reapplied first and updated the checksum record that both
   patches shared. This patch then saw "target unchanged" and skipped
   reapplying. bray stayed patched.
2. For about two days, ace and clovis received migrations without the
   post-receive unmount. tower migrated ace to clovis, then clovis to ace.
   containerd started on a stale view of `meta.db` and wrote on top of it for
   hours, which corrupted the on-disk file.
3. Once both patches were reapplied (each now keeps its own checksum record),
   the next migration moved the already-corrupt bytes to a freshly rebooted
   node. That node had no stale cache, so containerd panicked right away. The
   damage was done before the patch came back.
4. Later migrations with the patch in place (helm 3 times, tower once) were
   clean.

The patch health alert missed the lapse because it read only the status file,
which still said `patched`. The `homelab_pve_patch_healthy` metric now also
checks that each patched file still contains its patch marker.

This supports the target-side stale-receive model above: the failure comes back
whenever the patch lapses, and it does not depend on the source side.

## Operational Files

- Script: `/usr/local/sbin/homelab-pve-zfs-recv-cache-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-zfs-recv-cache-patch`
- Status: `/var/lib/homelab/pve-zfs-recv-cache-patch/status`
- Backups: `/var/backups/homelab/pve-zfs-recv-cache-patch/`

The apt hook runs the patch script with `--restart-services`, which uses
`systemctl try-restart pvedaemon.service pve-ha-lrm.service pvescheduler.service`
after reapplying the patch. This prevents long-lived PVE daemons, including the
scheduled replication runner, from keeping superseded Perl module code loaded
after package updates.

Patch reapply uses the shared lock `/run/lock/homelab-pve-patches.lock`, so it
waits for other homelab PVE patch hooks before editing Proxmox Perl files or
restarting services.

Remove this module and revert the local patch when Proxmox/OpenZFS ships an
equivalent upstream fix.
