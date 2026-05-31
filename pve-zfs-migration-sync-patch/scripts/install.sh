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

TARGET=/usr/share/perl5/PVE/Storage.pm
STATE_DIR=/var/lib/homelab/pve-zfs-migration-sync-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-migration-sync-patch
STATUS_FILE=${STATE_DIR}/status

ORIGINAL=$(cat <<'EOF'
    volume_snapshot($cfg, $volid, $snapshot) if $migration_snapshot;
EOF
)

PATCHED=$(cat <<'EOF'
    if ($migration_snapshot) {
        my ($sid) = parse_volume_id($volid);
        my $scfg = storage_config($cfg, $sid);
        PVE::Tools::run_command(['zpool', 'sync', $scfg->{pool}])
            if $scfg->{type} eq 'zfspool';
        volume_snapshot($cfg, $volid, $snapshot);
    }
EOF
)

write_status() {
  local state=$1
  local package_version
  package_version=$(dpkg-query -W -f='${Version}' libpve-storage-perl 2>/dev/null || true)
  {
    printf 'state=%s\n' "${state}"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'target=%s\n' "${TARGET}"
    printf 'package=libpve-storage-perl\n'
    printf 'package_version=%s\n' "${package_version}"
    grep -nF "volume_snapshot(
" "${TARGET}" || true
    grep -nF "zpool', 'sync'" "${TARGET}" || true
  } > "${STATUS_FILE}"
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

if [[ ! -f ${TARGET} ]]; then
  echo "missing target: ${TARGET}" >&2
  {
    printf 'state=failed-missing-target\n'
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'target=%s\n' "${TARGET}"
  } > "${STATUS_FILE}"
  exit 1
fi

patched_count=$(grep -Fc "zpool', 'sync'" "${TARGET}" || true)
original_count=$(grep -Fc "volume_snapshot(\$cfg, \$volid, \$snapshot) if \$migration_snapshot;" "${TARGET}" || true)

if [[ ${patched_count} -ge 1 && ${original_count} -eq 0 ]]; then
  state=already-patched
elif [[ ${patched_count} -eq 0 && ${original_count} -eq 1 ]]; then
  backup="${BACKUP_DIR}/Storage.pm.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp "${TARGET}" "${backup}"
  PATCHED_BLOCK=${PATCHED} ORIGINAL_LINE=${ORIGINAL} perl -0pi -e 's/\Q$ENV{ORIGINAL_LINE}\E/$ENV{PATCHED_BLOCK}/' "${TARGET}"
  state=patched
else
  echo "unexpected migration snapshot stanza in ${TARGET}; refusing to patch" >&2
  grep -nF "volume_snapshot($cfg, $volid, $snapshot)" "${TARGET}" >&2 || true
  write_status failed-unexpected-line
  exit 1
fi

write_status "${state}"
cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab ZFS migration sync patch after package updates.
DPkg::Post-Invoke { "${PATCH_SCRIPT} >/var/log/homelab-pve-zfs-migration-sync-patch.log 2>&1 || true"; };
EOF
chmod 0644 "${APT_HOOK}"

"${PATCH_SCRIPT}"
