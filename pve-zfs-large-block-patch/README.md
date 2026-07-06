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
fail with `incremental send stream requires -L`.

## Recovery: patch-only vs. destroy+reseed

When replication fails with `incremental send stream requires -L`, **do not
destroy the replica by default.** First determine which of two distinct states
you are in, because only one of them needs a reseed:

1. **Patch was reverted, data on disk is fine (patch-only fix).** The replica
   was already received with large blocks, but the source's send command lost
   `-L` (e.g. a `libpve-storage-perl` upgrade reverted the patch — the
   2026-07-05 incident). Diagnostic:
   - `zpool get feature@large_blocks <pool>` is `active` on **both** source and
     destination pools, and
   - `zfs get recordsize <dataset>` is `1M` on both.
   Fix: reapply the patch on the **current source** node
   (`/usr/local/sbin/homelab-pve-zfs-large-block-patch`), confirm the send line
   reads `zfs send -RpvUL`, then re-run replication (`pvesr run --id <job>` on
   the source). No destroy, no reseed. **This is the common case; the ~14.6 TB
   media replica must not be destroyed for it.**

2. **First large-block stream was genuinely received as 128K (destroy+reseed).**
   The destination truly stored the data with 128K blocks because the very first
   1M stream was sent without `-L`. Signs: destination pool shows
   `feature@large_blocks` **not** `active`, or replication still fails with
   `requires -L` **after** the patch is confirmed live on the source and a full
   send is attempted. Only then destroy the destination replica for that disk
   and let PVE reseed it while the patch is active.

A fresh full reseed only needs to happen (case 2) while this patch is active so
the receiver activates `feature@large_blocks` correctly. In case 1 the receiver
already has it active and a patched incremental resync is sufficient.

Upstream tracking: Bug 4603 - Add support for migrating ZFS datasets with
large_blocks: https://bugzilla.proxmox.com/show_bug.cgi?id=4603

Remove this module and revert the local patch when Proxmox ships equivalent
upstream behavior for this bug.

Deployment also installs `/usr/local/sbin/homelab-pve-zfs-large-block-patch`
and an apt `DPkg::Post-Invoke` hook so `libpve-storage-perl` package upgrades
that replace `ZFSPoolPlugin.pm` are patched again automatically. The next
forked replication or migration worker loads the patched module from disk; no
Proxmox service restart is required.

The reapply is **deferred**: the hook uses `systemd-run --on-active=30s` to run
the patch script as a transient unit ~30s after the apt transaction finishes,
rather than inline inside dpkg. This avoids the failure mode observed on
2026-07-05, where a `libpve-storage-perl` 9.1.5 -> 9.1.6 upgrade replaced
`ZFSPoolPlugin.pm` with the unpatched `-RpvU` version but the inline in-dpkg
Post-Invoke reapply did not take effect (it contended on the shared patch lock
held by the sibling PVE patch hooks in the same run and/or raced the file
replacement), leaving replication of the 1M-recordsize media dataset sending
without `-L` and the next HA pre-stop replication failing. Running after the
transaction completes and the shared lock clears makes the reapply reliable. If
`systemd-run` is unavailable the hook falls back to the previous inline call.

Patch reapply uses the shared lock `/run/lock/homelab-pve-patches.lock`, so it
waits for other homelab PVE patch hooks before editing Proxmox Perl files.

Operational files:

- Script: `/usr/local/sbin/homelab-pve-zfs-large-block-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-zfs-large-block-patch`
- Status: `/var/lib/homelab/pve-zfs-large-block-patch/status`
- Backups: `/var/backups/homelab/pve-zfs-large-block-patch/`
