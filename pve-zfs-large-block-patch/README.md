# PVE ZFS Large-Block Patch

Adds `-L` to Proxmox's ZFS replication send command in
`/usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm`:

```perl
['zfs', 'send', '-RpvUL']
```

This is required for PVE replication of datasets with large blocks, such as
`recordsize=1M` media datasets. A fresh full reseed must happen while this
patch is active so the receiver activates `feature@large_blocks` correctly.

Remove this module when Proxmox ships equivalent upstream behavior.

Deployment also installs `/usr/local/sbin/homelab-pve-zfs-large-block-patch`
and an apt `DPkg::Post-Invoke` hook so `libpve-storage-perl` package upgrades
that replace `ZFSPoolPlugin.pm` are patched again automatically.

Operational files:

- Script: `/usr/local/sbin/homelab-pve-zfs-large-block-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-zfs-large-block-patch`
- Status: `/var/lib/homelab/pve-zfs-large-block-patch/status`
- Backups: `/var/backups/homelab/pve-zfs-large-block-patch/`
