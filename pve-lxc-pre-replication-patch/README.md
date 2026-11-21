# PVE LXC Pre-Replication Migration Patch

Adds a pre-stop replication pass for Proxmox LXC restart migrations when a local
PVE replication job already targets the migration destination.

## Patched Files

- `/usr/share/perl5/PVE/LXC/Migrate.pm`
  - Function: `PVE::LXC::Migrate::prepare`
  - Runs the existing local replication job before shutting down a running CT in
    restart migration mode. Proxmox still performs its normal final replication
    after shutdown, so the stopped window only needs to transfer the final delta.
- `/usr/share/perl5/PVE/HA/Resources/PVECT.pm`
  - Function: `PVE::HA::Resources::PVECT::migrate`
  - Stops pre-shutting down HA-managed CTs in the HA resource plugin. Instead it
    calls the normal LXC migration API with `restart => 1`, allowing the patched
    LXC migration worker to run pre-stop replication before shutdown.

## Why

Stock PVE restart migration for running LXCs stops the container before local
volume transfer. For large ZFS-backed LXCs, especially HDD-backed subvolumes,
this makes the whole incremental replication window downtime.

With this patch and an existing local PVE replication job to the target node, the
sequence becomes:

1. Run replication while the CT is still online.
2. Stop the CT.
3. Run Proxmox's existing final replication while stopped.
4. Move config and start the CT on the target.

## Scope

This patch only changes migrations where PVE already has a matching local
replication job for the selected target. It does not create temporary replication
jobs for arbitrary targets and does not implement true LXC live migration.

## Upstream Tracking

- Forum: https://forum.proxmox.com/threads/improvement-reduce-migration-downtime-to-seconds-with-two-step-transfer.72498/
- Bugzilla: https://bugzilla.proxmox.com/show_bug.cgi?id=2984

The patched blocks in `scripts/install.sh` follow the two-patch series prepared
for `pve-devel` (`[PATCH container 1/2]` for `Migrate.pm`,
`[PATCH ha-manager 2/2]` for `PVECT.pm`), so production soak-tests the code
under review. They are written against pve-container 6.1.24 and pve-ha-manager
5.2.6; rebase the series whenever the blocks are rebased.

## When The Reapply Refuses

The installer replaces exact upstream stanzas, so any upstream edit to either
one makes the apt-hook reapply refuse with `failed-unexpected-lxc_migrate` or
`failed-unexpected-ha_pvect`, and `PvePatchUnhealthy` fires. Both files are then
stock: nothing breaks, but HA CT migrations lose the pre-stop replication pass.

Read the package changelog before acting. If it says bug #2984 was fixed,
retire the module. Otherwise upstream only moved code around the stanza:
copy the new stanza into `LXC_ORIGINAL`/`HA_ORIGINAL`, rebuild the patched
blocks around it, refresh `tests/fixtures/pve_lxc_pre_replication/` from the
new packages, and redeploy. pve-container 6.1.24 (an `eval` around `vm_stop`)
and pve-ha-manager 5.2.6 (`$migrate_options` passthrough) were the first such
rebase.

## Operational Files

- Script: `/usr/local/sbin/homelab-pve-lxc-pre-replication-patch`
- Apt hook: `/etc/apt/apt.conf.d/99-homelab-pve-lxc-pre-replication-patch`
- Status: `/var/lib/homelab/pve-lxc-pre-replication-patch/status`
- Backups: `/var/backups/homelab/pve-lxc-pre-replication-patch/`

The apt hook reapplies the patch after package updates and restarts
`pvedaemon.service` plus `pve-ha-lrm.service` so long-lived PVE daemons load the
patched Perl modules.

Patch reapply uses the shared lock `/run/lock/homelab-pve-patches.lock`, so it
waits for other homelab PVE patch hooks before editing Proxmox Perl files or
restarting services.
