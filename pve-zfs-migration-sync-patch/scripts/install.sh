#!/usr/bin/env bash
set -euo pipefail

STATE_DIR=/var/lib/homelab/pve-zfs-migration-sync-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-migration-sync-patch
PATCH_SCRIPT=/usr/local/sbin/homelab-pve-zfs-migration-sync-patch
APT_HOOK=/etc/apt/apt.conf.d/99-homelab-pve-zfs-migration-sync-patch

if [[ ${EUID} -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

cat > "${PATCH_SCRIPT}" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

TARGET_STORAGE=/usr/share/perl5/PVE/Storage.pm
TARGET_REPLICATION=/usr/share/perl5/PVE/Replication.pm
TARGET_MIGRATE=/usr/share/perl5/PVE/LXC/Migrate.pm
STATE_DIR=/var/lib/homelab/pve-zfs-migration-sync-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-migration-sync-patch
STATUS_FILE=${STATE_DIR}/status
PVE_SERVICES=(pvescheduler pvedaemon pvestatd pve-ha-lrm)

# ---------------------------------------------------------------------------
# Storage.pm patch: non-replicated migration path
#   Adds zpool sync before volume_snapshot() in $volume_export_prepare so
#   migration snapshots taken via storage_migrate() flush dirty TXG data first.
# ---------------------------------------------------------------------------
STORAGE_ORIGINAL=$(cat <<'EOF'
    volume_snapshot($cfg, $volid, $snapshot) if $migration_snapshot;
EOF
)

STORAGE_PATCHED=$(cat <<'EOF'
    if ($migration_snapshot) {
        my ($sid) = parse_volume_id($volid);
        my $scfg = storage_config($cfg, $sid);
        PVE::Tools::run_command(['zpool', 'sync', $scfg->{pool}])
            if $scfg->{type} eq 'zfspool';
        volume_snapshot($cfg, $volid, $snapshot);
    }
EOF
)

# ---------------------------------------------------------------------------
# Replication.pm patch: replicated migration path
#   Adds zpool sync per unique pool before volume_snapshot() in replicate() so
#   that after the dataset is unmounted and kernel page-cache pages are flushed
#   to ZFS, the pool is also synced to disk before the snapshot is taken.
# ---------------------------------------------------------------------------
REPLICATION_ORIGINAL=$(cat <<'EOF'
    my $replicate_snapshots = {};
    eval {
        foreach my $volid (@$sorted_volids) {
            $logfunc->("create snapshot '${sync_snapname}' on $volid");
            PVE::Storage::volume_snapshot($storecfg, $volid, $sync_snapname);
            $replicate_snapshots->{$volid}->{$sync_snapname} = 1;
        }
    };
EOF
)

REPLICATION_PATCHED=$(cat <<'EOF'
    my %synced_pools;
    foreach my $volid (@$sorted_volids) {
        my ($storeid) = PVE::Storage::parse_volume_id($volid);
        my $scfg = PVE::Storage::storage_config($storecfg, $storeid);
        if ($scfg->{type} eq 'zfspool' && !$synced_pools{$scfg->{pool}}) {
            $logfunc->("zpool sync '$scfg->{pool}' before snapshot");
            PVE::Tools::run_command(['zpool', 'sync', $scfg->{pool}]);
            $synced_pools{$scfg->{pool}} = 1;
        }
    }

    my $replicate_snapshots = {};
    eval {
        foreach my $volid (@$sorted_volids) {
            $logfunc->("create snapshot '${sync_snapname}' on $volid");
            PVE::Storage::volume_snapshot($storecfg, $volid, $sync_snapname);
            $replicate_snapshots->{$volid}->{$sync_snapname} = 1;
        }
    };
EOF
)

# ---------------------------------------------------------------------------
# LXC/Migrate.pm patch: fix snapshot ordering
#   Moves run_replication() to after umount_all() + deactivate_volumes() so
#   the ZFS dataset is unmounted before the migration snapshot is taken.
#   Unmounting flushes all kernel page-cache dirty pages (including bbolt mmap
#   writes) to the ZFS vnode.  Without this, zpool sync has nothing to flush
#   for pages that are dirty in the page cache but not yet written to ZFS.
# ---------------------------------------------------------------------------
MIGRATE_ORIGINAL=$(cat <<'EOF'
    if ($remote) {
        die "cannot remote-migrate replicated VM\n"
            if $rep_cfg->check_for_existing_jobs($vmid, 1);
    } elsif (my $jobcfg = $rep_cfg->find_local_replication_job($vmid, $self->{node})) {
        die "can't live migrate VM with replicated volumes\n" if $self->{running};
        my $start_time = time();
        my $logfunc = sub { my ($msg) = @_; $self->log('info', $msg); };
        $rep_volumes = PVE::Replication::run_replication(
            'PVE::LXC::Config', $jobcfg, $start_time, $start_time, $logfunc,
        );
    }

    my $opts = $self->{opts};
    foreach my $volid (keys %$volhash) {
        next if $rep_volumes->{$volid};
EOF
)

MIGRATE_PATCHED=$(cat <<'EOF'
    if ($remote) {
        die "cannot remote-migrate replicated VM\n"
            if $rep_cfg->check_for_existing_jobs($vmid, 1);
    }

    my $opts = $self->{opts};
    foreach my $volid (keys %$volhash) {
        next if $rep_volumes && $rep_volumes->{$volid};
EOF
)

MIGRATE_UMOUNT_ORIGINAL=$(cat <<'EOF'
    PVE::LXC::umount_all($vmid, $self->{storecfg}, $conf);

    #to be sure there are no active volumes
    my $vollist = PVE::LXC::Config->get_vm_volumes($conf);
    PVE::Storage::deactivate_volumes($self->{storecfg}, $vollist);

    if ($remote) {
EOF
)

MIGRATE_UMOUNT_PATCHED=$(cat <<'EOF'
    PVE::LXC::umount_all($vmid, $self->{storecfg}, $conf);

    #to be sure there are no active volumes
    my $vollist = PVE::LXC::Config->get_vm_volumes($conf);
    PVE::Storage::deactivate_volumes($self->{storecfg}, $vollist);

    # Run replication after unmount so all kernel page-cache dirty pages
    # (including bbolt mmap writes) are flushed to the ZFS vnode before the
    # migration snapshot is taken.  Previously this ran before umount_all(),
    # causing the snapshot to capture stale data still in the page cache.
    if (!$remote) {
        if (my $jobcfg = $rep_cfg->find_local_replication_job($vmid, $self->{node})) {
            die "can't live migrate VM with replicated volumes\n" if $self->{running};
            my $start_time = time();
            my $logfunc = sub { my ($msg) = @_; $self->log('info', $msg); };
            $rep_volumes = PVE::Replication::run_replication(
                'PVE::LXC::Config', $jobcfg, $start_time, $start_time, $logfunc,
            );
        }
    }

    if ($remote) {
EOF
)

write_status() {
  local storage_state=$1
  local replication_state=$2
  local migrate_state=$3
  local restart_state=${4:-not-run}
  local storage_ver replication_ver migrate_ver
  storage_ver=$(dpkg-query -W -f='${Version}' libpve-storage-perl 2>/dev/null || true)
  replication_ver=$(dpkg-query -W -f='${Version}' pve-container 2>/dev/null || true)
  migrate_ver=${replication_ver}
  {
    printf 'storage_state=%s\n' "${storage_state}"
    printf 'replication_state=%s\n' "${replication_state}"
    printf 'migrate_state=%s\n' "${migrate_state}"
    printf 'services_restart=%s\n' "${restart_state}"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'storage_target=%s\n' "${TARGET_STORAGE}"
    printf 'replication_target=%s\n' "${TARGET_REPLICATION}"
    printf 'migrate_target=%s\n' "${TARGET_MIGRATE}"
    printf 'package_libpve_storage=%s\n' "${storage_ver}"
    printf 'package_pve_container=%s\n' "${replication_ver}"
    printf 'package_pve_container_migrate=%s\n' "${migrate_ver}"
    grep -nF "zpool', 'sync'" "${TARGET_STORAGE}" || true
    grep -nF "zpool sync" "${TARGET_REPLICATION}" || true
    grep -nF "Run replication after unmount" "${TARGET_MIGRATE}" || true
  } > "${STATUS_FILE}"
}

restart_pve_services() {
  # Proxmox Perl daemons keep modules loaded in memory; restart them so
  # Storage.pm, Replication.pm, and LXC/Migrate.pm changes take effect.
  # pve-ha-lrm drives run_replication() and vzmigrate during HA migrate.
  systemctl try-restart "${PVE_SERVICES[@]}"
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

# ---------------------------------------------------------------------------
# Patch Storage.pm
# ---------------------------------------------------------------------------
if [[ ! -f ${TARGET_STORAGE} ]]; then
  echo "missing target: ${TARGET_STORAGE}" >&2
  write_status failed-missing-target skipped skipped not-run
  exit 1
fi

storage_patched_count=$(grep -Fc "zpool', 'sync'" "${TARGET_STORAGE}" || true)
storage_original_count=$(grep -Fc "volume_snapshot(\$cfg, \$volid, \$snapshot) if \$migration_snapshot;" "${TARGET_STORAGE}" || true)

if [[ ${storage_patched_count} -ge 1 && ${storage_original_count} -eq 0 ]]; then
  storage_state=already-patched
elif [[ ${storage_patched_count} -eq 0 && ${storage_original_count} -eq 1 ]]; then
  backup="${BACKUP_DIR}/Storage.pm.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp "${TARGET_STORAGE}" "${backup}"
  PATCHED_BLOCK=${STORAGE_PATCHED} ORIGINAL_LINE=${STORAGE_ORIGINAL} \
    perl -0pi -e 's/\Q$ENV{ORIGINAL_LINE}\E/$ENV{PATCHED_BLOCK}/' "${TARGET_STORAGE}"
  storage_state=patched
else
  echo "unexpected migration snapshot stanza in ${TARGET_STORAGE}; refusing to patch" >&2
  write_status failed-unexpected-line skipped skipped not-run
  exit 1
fi

# ---------------------------------------------------------------------------
# Patch Replication.pm
# ---------------------------------------------------------------------------
if [[ ! -f ${TARGET_REPLICATION} ]]; then
  echo "missing target: ${TARGET_REPLICATION}" >&2
  write_status "${storage_state}" failed-missing-target skipped not-run
  exit 1
fi

replication_patched_count=$(grep -Fc "zpool sync" "${TARGET_REPLICATION}" || true)
replication_original_count=$(grep -Fc 'PVE::Storage::volume_snapshot($storecfg, $volid, $sync_snapname);' "${TARGET_REPLICATION}" || true)

if [[ ${replication_patched_count} -ge 1 ]]; then
  replication_state=already-patched
elif [[ ${replication_patched_count} -eq 0 && ${replication_original_count} -ge 1 ]]; then
  backup="${BACKUP_DIR}/Replication.pm.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp "${TARGET_REPLICATION}" "${backup}"
  PATCHED_BLOCK=${REPLICATION_PATCHED} ORIGINAL_LINE=${REPLICATION_ORIGINAL} \
    perl -0pi -e 's/\Q$ENV{ORIGINAL_LINE}\E/$ENV{PATCHED_BLOCK}/' "${TARGET_REPLICATION}"
  replication_state=patched
else
  echo "unexpected replication snapshot stanza in ${TARGET_REPLICATION}; refusing to patch" >&2
  write_status "${storage_state}" failed-unexpected-line skipped not-run
  exit 1
fi

# ---------------------------------------------------------------------------
# Patch LXC/Migrate.pm (two substitutions: remove early run_replication,
# add it back after umount_all + deactivate_volumes)
# ---------------------------------------------------------------------------
if [[ ! -f ${TARGET_MIGRATE} ]]; then
  echo "missing target: ${TARGET_MIGRATE}" >&2
  write_status "${storage_state}" "${replication_state}" failed-missing-target not-run
  exit 1
fi

migrate_patched_count=$(grep -Fc "Run replication after unmount" "${TARGET_MIGRATE}" || true)
migrate_original_count=$(grep -Fc "find_local_replication_job" "${TARGET_MIGRATE}" || true)

if [[ ${migrate_patched_count} -ge 1 ]]; then
  migrate_state=already-patched
elif [[ ${migrate_patched_count} -eq 0 && ${migrate_original_count} -ge 1 ]]; then
  backup="${BACKUP_DIR}/Migrate.pm.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp "${TARGET_MIGRATE}" "${backup}"
  # First substitution: remove run_replication from before storage_migrate loop
  PATCHED_BLOCK=${MIGRATE_PATCHED} ORIGINAL_LINE=${MIGRATE_ORIGINAL} \
    perl -0pi -e 's/\Q$ENV{ORIGINAL_LINE}\E/$ENV{PATCHED_BLOCK}/' "${TARGET_MIGRATE}"
  # Second substitution: insert run_replication after umount_all + deactivate_volumes
  PATCHED_BLOCK=${MIGRATE_UMOUNT_PATCHED} ORIGINAL_LINE=${MIGRATE_UMOUNT_ORIGINAL} \
    perl -0pi -e 's/\Q$ENV{ORIGINAL_LINE}\E/$ENV{PATCHED_BLOCK}/' "${TARGET_MIGRATE}"
  migrate_state=patched
else
  echo "unexpected migrate stanza in ${TARGET_MIGRATE}; refusing to patch" >&2
  write_status "${storage_state}" "${replication_state}" failed-unexpected-line not-run
  exit 1
fi

# ---------------------------------------------------------------------------
# Restart services so in-memory modules are replaced
# ---------------------------------------------------------------------------
restart_state=restarted
if ! restart_pve_services; then
  restart_state=failed
  write_status "${storage_state}" "${replication_state}" "${migrate_state}" "${restart_state}"
  echo "failed to restart Proxmox services: ${PVE_SERVICES[*]}" >&2
  exit 1
fi

write_status "${storage_state}" "${replication_state}" "${migrate_state}" "${restart_state}"
cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab ZFS migration sync patch after package updates.
DPkg::Post-Invoke { "${PATCH_SCRIPT} >/var/log/homelab-pve-zfs-migration-sync-patch.log 2>&1 || true"; };
EOF
chmod 0644 "${APT_HOOK}"

"${PATCH_SCRIPT}"
