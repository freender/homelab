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
STATE_DIR=/var/lib/homelab/pve-zfs-migration-sync-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-migration-sync-patch
STATUS_FILE=${STATE_DIR}/status

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
#   For CTs with a PVE replication job, LXC/Migrate.pm calls run_replication()
#   and skips storage_migrate() entirely, so the Storage.pm fix is never
#   reached.  This patch fixes replicate() directly with two steps:
#
#   syncfs() on each ZFS volume mountpoint via `sync --file-system <path>`.
#   Flushes kernel page-cache dirty pages to the ZFS vnode.  This covers
#   mmap writers (bbolt, sqlite WAL, etc.) that wrote after guest shutdown
#   but whose pages have not yet been pushed down to ZFS.  zpool sync is
#   not needed: zfs snapshot is itself a TXG-committed operation that forces
#   a TXG sync, so once syncfs pushes the pages into ZFS's dirty TXG the
#   snapshot commit flushes them to disk atomically.
#
#   Flush chain: page cache -> ZFS vnode (syncfs) -> zfs snapshot (TXG commit).
#   This is the general fix: no hookscript or guest-specific knowledge needed.
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
    foreach my $volid (@$sorted_volids) {
        my ($storeid) = PVE::Storage::parse_volume_id($volid);
        my $scfg = PVE::Storage::storage_config($storecfg, $storeid);
        next if $scfg->{type} ne 'zfspool';
        my $path = PVE::Storage::path($storecfg, $volid);
        if (defined($path) && -d $path) {
            $logfunc->("syncfs '$path' before snapshot");
            PVE::Tools::run_command(['/usr/bin/sync', '--file-system', $path]);
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

write_status() {
  local storage_state=$1
  local replication_state=$2
  local storage_ver replication_ver
  storage_ver=$(dpkg-query -W -f='${Version}' libpve-storage-perl 2>/dev/null || true)
  replication_ver=$(dpkg-query -W -f='${Version}' pve-container 2>/dev/null || true)
  {
    printf 'storage_state=%s\n' "${storage_state}"
    printf 'replication_state=%s\n' "${replication_state}"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'storage_target=%s\n' "${TARGET_STORAGE}"
    printf 'replication_target=%s\n' "${TARGET_REPLICATION}"
    printf 'package_libpve_storage=%s\n' "${storage_ver}"
    printf 'package_pve_container=%s\n' "${replication_ver}"
    grep -nF "zpool', 'sync'" "${TARGET_STORAGE}" || true
    grep -nF "syncfs" "${TARGET_REPLICATION}" || true
  } > "${STATUS_FILE}"
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

# ---------------------------------------------------------------------------
# Patch Storage.pm
# ---------------------------------------------------------------------------
if [[ ! -f ${TARGET_STORAGE} ]]; then
  echo "missing target: ${TARGET_STORAGE}" >&2
  write_status failed-missing-target skipped
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
  write_status failed-unexpected-line skipped
  exit 1
fi

# ---------------------------------------------------------------------------
# Patch Replication.pm
# ---------------------------------------------------------------------------
if [[ ! -f ${TARGET_REPLICATION} ]]; then
  echo "missing target: ${TARGET_REPLICATION}" >&2
  write_status "${storage_state}" failed-missing-target
  exit 1
fi

replication_patched_count=$(grep -Fc "syncfs '\$path' before snapshot" "${TARGET_REPLICATION}" || true)
replication_original_count=$(grep -Fc "my \$replicate_snapshots = {};" "${TARGET_REPLICATION}" || true)

if [[ ${replication_patched_count} -ge 1 ]]; then
  replication_state=already-patched
elif [[ ${replication_patched_count} -eq 0 && ${replication_original_count} -eq 1 ]]; then
  backup="${BACKUP_DIR}/Replication.pm.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp "${TARGET_REPLICATION}" "${backup}"
  PATCHED_BLOCK=${REPLICATION_PATCHED} ORIGINAL_BLOCK=${REPLICATION_ORIGINAL} \
    perl -0pi -e 's/\Q$ENV{ORIGINAL_BLOCK}\E/$ENV{PATCHED_BLOCK}/' "${TARGET_REPLICATION}"
  replication_state=patched
else
  echo "unexpected replication snapshot stanza in ${TARGET_REPLICATION}; refusing to patch" >&2
  write_status "${storage_state}" failed-unexpected-line
  exit 1
fi

# Forked replication and migration workers load these modules from disk on the
# next job, so no Proxmox service restart is required.
write_status "${storage_state}" "${replication_state}"
cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab ZFS migration sync patch after package updates.
DPkg::Post-Invoke { "${PATCH_SCRIPT} >/var/log/homelab-pve-zfs-migration-sync-patch.log 2>&1 || true"; };
EOF
chmod 0644 "${APT_HOOK}"

"${PATCH_SCRIPT}"
