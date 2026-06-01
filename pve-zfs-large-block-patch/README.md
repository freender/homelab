# PVE ZFS Large-Block Patch

Adds `-L` to Proxmox's ZFS replication send command in
`/usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm`:

```perl
['zfs', 'send', '-RpvUL']
```

This is required for PVE replication of datasets with large blocks, such as
`recordsize=1M` media datasets. A fresh full reseed must happen while this
patch is active so the receiver activates `feature@large_blocks` correctly.

If a dataset is changed from the default record size to `recordsize=1M`, the
first replication stream that contains newly written 1M blocks must be sent
with `-L`. Sending that first large-block stream without `-L` causes the
receiver to store the data as 128K blocks; later reverse incrementals can then
fail with `incremental send stream requires -L`. Recovery from that state is to
destroy the destination replica and reseed it while this patch is active.

Upstream tracking: Bug 4603 - Add support for migrating ZFS datasets with
large_blocks: https://bugzilla.proxmox.com/show_bug.cgi?id=4603

Remove this module and revert the local patch when Proxmox ships equivalent
upstream behavior for this bug.

Deployment also installs `/usr/local/sbin/homelab-pve-zfs-large-block-patch`
and an apt `DPkg::Post-Invoke` hook so `libpve-storage-perl` package upgrades
that replace `ZFSPoolPlugin.pm` are patched again automatically. The patch
script restarts `pvescheduler`, `pvedaemon`, and `pvestatd` after patch
verification so long-lived Proxmox Perl daemons reload `ZFSPoolPlugin.pm` before
scheduled replication or migration uses the code path.

Operational files:

- Script: `/usr/local/sbin/homelab-pve-zfs-large-block-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-zfs-large-block-patch`
- Status: `/var/lib/homelab/pve-zfs-large-block-patch/status`
- Backups: `/var/backups/homelab/pve-zfs-large-block-patch/`
- Reloaded services: `pvescheduler`, `pvedaemon`, `pvestatd`
