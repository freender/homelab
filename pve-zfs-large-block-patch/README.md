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
