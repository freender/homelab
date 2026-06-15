#!/usr/bin/env bash
set -euo pipefail

STATE_DIR=/var/lib/homelab/pve-zfs-recv-cache-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-recv-cache-patch
PATCH_SCRIPT=/usr/local/sbin/homelab-pve-zfs-recv-cache-patch
APT_HOOK=/etc/apt/apt.conf.d/99-homelab-pve-zfs-recv-cache-patch

OLD_STATE_DIR=/var/lib/homelab/pve-zfs-migration-sync-patch
OLD_PATCH_SCRIPT=/usr/local/sbin/homelab-pve-zfs-migration-sync-patch
OLD_APT_HOOK=/etc/apt/apt.conf.d/99-homelab-pve-zfs-migration-sync-patch

if [[ ${EUID} -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

cat > "${PATCH_SCRIPT}" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

RESTART_SERVICES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart-services)
      RESTART_SERVICES=true
      ;;
    *)
      echo "usage: $0 [--restart-services]" >&2
      exit 2
      ;;
  esac
  shift
done

TARGET=/usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm
OLD_TARGET_STORAGE=/usr/share/perl5/PVE/Storage.pm
OLD_TARGET_REPLICATION=/usr/share/perl5/PVE/Replication.pm
STATE_DIR=/var/lib/homelab/pve-zfs-recv-cache-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-recv-cache-patch
STATUS_FILE=${STATE_DIR}/status

ORIGINAL=$(cat <<'EOF'
    eval {
        run_command(['zfs', 'recv', '-F', '-x', 'encryption', '--', $zfspath],
            input => "<&$fd");
    };
EOF
)

PATCHED=$(cat <<'EOF'
    my $unmount_received_subvol = sub {
        return if $volume_format ne 'subvol';

        eval { run_command(['zfs', 'unmount', '--', $zfspath]); };
        if (my $err = $@) {
            die $err if $err !~ m/not currently mounted/;
        }
    };

    # Receiving into a mounted subvol can leave stale cached file contents visible
    # through the live mount. Unmount after receive so activation remounts it.

    eval {
        run_command(['zfs', 'recv', '-F', '-x', 'encryption', '--', $zfspath],
            input => "<&$fd");
        $unmount_received_subvol->();
    };
EOF
)

STORAGE_ORIGINAL=$(cat <<'EOF'
    volume_snapshot($cfg, $volid, $snapshot) if $migration_snapshot;
EOF
)

STORAGE_ZPOOL_SYNC_PATCHED=$(cat <<'EOF'
    if ($migration_snapshot) {
        my ($sid) = parse_volume_id($volid);
        my $scfg = storage_config($cfg, $sid);
        PVE::Tools::run_command(['zpool', 'sync', $scfg->{pool}])
            if $scfg->{type} eq 'zfspool';
        volume_snapshot($cfg, $volid, $snapshot);
    }
EOF
)

STORAGE_SYNCFS_PATCHED=$(cat <<'EOF'
    if ($migration_snapshot) {
        my ($sid) = parse_volume_id($volid);
        my $scfg = storage_config($cfg, $sid);
        if ($scfg->{type} eq 'zfspool') {
            my $path = path($cfg, $volid);
            if (defined($path) && -d $path) {
                PVE::Tools::run_command(['/usr/bin/sync', '--file-system', $path]);
            }
        }
        volume_snapshot($cfg, $volid, $snapshot);
    }
EOF
)

REPLICATION_SYNCFS_PATCHED=$(cat <<'EOF'

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
    printf 'mitigation=unmount-subvol-after-zfs-receive\n'
    grep -nF 'unmount_received_subvol' "${TARGET}" || true
  } > "${STATUS_FILE}"
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

cleanup_source_sync_patches() {
  local timestamp
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)

  if [[ -f ${OLD_TARGET_STORAGE} ]]; then
    if grep -Fq "zpool', 'sync'" "${OLD_TARGET_STORAGE}" \
      || grep -Fq "'/usr/bin/sync', '--file-system'" "${OLD_TARGET_STORAGE}"; then
      cp "${OLD_TARGET_STORAGE}" "${BACKUP_DIR}/Storage.pm.remove-source-sync.${timestamp}.bak"
      STORAGE_ORIGINAL_BLOCK=${STORAGE_ORIGINAL} STORAGE_ZPOOL_SYNC_BLOCK=${STORAGE_ZPOOL_SYNC_PATCHED} \
        perl -0pi -e 's/\Q$ENV{STORAGE_ZPOOL_SYNC_BLOCK}\E/$ENV{STORAGE_ORIGINAL_BLOCK}/' "${OLD_TARGET_STORAGE}"
      STORAGE_ORIGINAL_BLOCK=${STORAGE_ORIGINAL} STORAGE_SYNCFS_BLOCK=${STORAGE_SYNCFS_PATCHED} \
        perl -0pi -e 's/\Q$ENV{STORAGE_SYNCFS_BLOCK}\E/$ENV{STORAGE_ORIGINAL_BLOCK}/' "${OLD_TARGET_STORAGE}"
    fi
  fi

  if [[ -f ${OLD_TARGET_REPLICATION} ]] && grep -Fq "syncfs '\$path' before snapshot" "${OLD_TARGET_REPLICATION}"; then
    cp "${OLD_TARGET_REPLICATION}" "${BACKUP_DIR}/Replication.pm.remove-source-sync.${timestamp}.bak"
    REPLICATION_SYNCFS_BLOCK=${REPLICATION_SYNCFS_PATCHED} \
      perl -0pi -e 's/\Q$ENV{REPLICATION_SYNCFS_BLOCK}\E//' "${OLD_TARGET_REPLICATION}"
  fi
}

if [[ ! -f ${TARGET} ]]; then
  echo "missing target: ${TARGET}" >&2
  write_status failed-missing-target
  exit 1
fi

patched_count=$(grep -Fc 'unmount_received_subvol' "${TARGET}" || true)
pre_unmount_count=$(grep -Fc '    $unmount_received_subvol->() if $exists;' "${TARGET}" || true)
post_unmount_count=$(grep -Fc '        $unmount_received_subvol->();' "${TARGET}" || true)
original_count=$(grep -Fc "run_command(['zfs', 'recv', '-F', '-x', 'encryption', '--', \$zfspath]" "${TARGET}" || true)

if [[ ${patched_count} -ge 1 && ${pre_unmount_count} -eq 0 && ${post_unmount_count} -ge 1 ]]; then
  state=already-patched
elif [[ ${patched_count} -ge 1 && ${original_count} -eq 1 ]]; then
  backup="${BACKUP_DIR}/ZFSPoolPlugin.pm.$(date -u +%Y%m%dT%H%M%SZ).partial.bak"
  cp "${TARGET}" "${backup}"
  if [[ ${pre_unmount_count} -gt 0 ]]; then
    perl -0pi -e 's/\n    \$unmount_received_subvol->\(\) if \$exists;\n//s' "${TARGET}"
    perl -0pi -e 's/# through the live mount\.[^\n]*/# through the live mount. Unmount after receive so activation remounts it./' "${TARGET}"
  fi
  if [[ ${post_unmount_count} -eq 0 ]]; then
    perl -0pi -e 's/(run_command\(\[\x27zfs\x27, \x27recv\x27, \x27-F\x27, \x27-x\x27, \x27encryption\x27, \x27--\x27, \$zfspath\],\n            input => "<&\$fd"\);)/$1\n        \$unmount_received_subvol->();/s' "${TARGET}"
  fi
  state=repaired-partial-patch
elif [[ ${patched_count} -eq 0 && ${original_count} -eq 1 ]]; then
  backup="${BACKUP_DIR}/ZFSPoolPlugin.pm.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp "${TARGET}" "${backup}"
  PATCHED_BLOCK=${PATCHED} ORIGINAL_BLOCK=${ORIGINAL} \
    perl -0pi -e 's/\Q$ENV{ORIGINAL_BLOCK}\E/$ENV{PATCHED_BLOCK}/' "${TARGET}"
  state=patched
else
  echo "unexpected ZFS receive stanza in ${TARGET}; refusing to patch" >&2
  grep -nF "['zfs', 'recv'" "${TARGET}" >&2 || true
  write_status failed-unexpected-line
  exit 1
fi

perl -c "${TARGET}" >/dev/null

cleanup_source_sync_patches

if [[ ${RESTART_SERVICES} == true ]]; then
  systemctl try-restart pvedaemon.service pve-ha-lrm.service
fi

# Replication and migration tasks run in forked workers, which load this module
# from disk on the next job. Package-triggered reapplies restart the relevant
# daemons so long-lived processes do not keep superseded Perl code loaded.
write_status "${state}"
cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab ZFS receive cache patch after package updates.
DPkg::Post-Invoke { "${PATCH_SCRIPT} --restart-services >/var/log/homelab-pve-zfs-recv-cache-patch.log 2>&1 || true"; };
EOF
chmod 0644 "${APT_HOOK}"

# Disable the superseded source-side syncfs patch so package upgrades do not
# reapply it. Backups remain under /var/backups/homelab/pve-zfs-migration-sync-patch.
rm -f "${OLD_PATCH_SCRIPT}" "${OLD_APT_HOOK}"
if [[ -d ${OLD_STATE_DIR} ]]; then
  cat > "${OLD_STATE_DIR}/status" <<EOF
storage_state=superseded
replication_state=superseded
timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
replaced_by=${PATCH_SCRIPT}
reason=target-side-zfs-receive-cache-mitigation
EOF
fi

"${PATCH_SCRIPT}"
