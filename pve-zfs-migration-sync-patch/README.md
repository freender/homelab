# PVE ZFS Migration Receive Cache Patch

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

## Superseded Patch

This module previously patched `Storage.pm` and `Replication.pm` to call
`syncfs()` before migration snapshots. Later testing showed the problem is not
source-side snapshot flushing. It is target-side stale live reads after receive.

Deploying this module removes the old local reapply script and apt hook:

- `/usr/local/sbin/homelab-pve-zfs-migration-sync-patch`
- `/etc/apt/apt.conf.d/99-homelab-pve-zfs-migration-sync-patch`

Old backups remain under:

- `/var/backups/homelab/pve-zfs-migration-sync-patch/`

## Upstream Tracking

Proxmox Bugzilla:

- Bug 7653 - LXC migration on zfspool snapshot may contain stale data
- https://bugzilla.proxmox.com/show_bug.cgi?id=7653

The current evidence likely also belongs upstream to OpenZFS/Linux because the
observable mismatch is live mounted file contents versus the same dataset's
snapshot contents after `zfs receive`.

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
