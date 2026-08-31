#!/usr/bin/env bash
set -euo pipefail

STATE_DIR=/var/lib/homelab/pve-zfs-recv-cache-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-recv-cache-patch
PATCH_SCRIPT=/usr/local/sbin/homelab-pve-zfs-recv-cache-patch
APT_HOOK=/etc/apt/apt.conf.d/99-homelab-pve-zfs-recv-cache-patch

if [[ ${EUID} -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

cat > "${PATCH_SCRIPT}" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

RESTART_SERVICES=false
IF_TARGET_CHANGED=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart-services)
      RESTART_SERVICES=true
      ;;
    --if-target-changed)
      IF_TARGET_CHANGED=true
      ;;
    *)
      echo "usage: $0 [--restart-services] [--if-target-changed]" >&2
      exit 2
      ;;
  esac
  shift
done

TARGET=/usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm
STATE_DIR=/var/lib/homelab/pve-zfs-recv-cache-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-recv-cache-patch
TARGET_CHECKSUM_DIR=/var/lib/homelab/pve-patches/target-checksums
STATUS_FILE=${STATE_DIR}/status
LOCK_FILE=/run/lock/homelab-pve-patches.lock

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

acquire_patch_lock() {
  mkdir -p "$(dirname "${LOCK_FILE}")"
  exec 200>"${LOCK_FILE}"
  if ! flock -n 200; then
    echo "waiting for shared homelab PVE patch lock: ${LOCK_FILE}"
    flock 200
  fi
}

target_checksum_file() {
  printf '%s/%s.checksum\n' "${TARGET_CHECKSUM_DIR}" "$(basename "$1")"
}

target_unchanged() {
  local target=$1
  local checksum_file
  checksum_file=$(target_checksum_file "${target}")
  [[ -f ${target} && -f ${checksum_file} ]] || return 1
  [[ $(cksum < "${target}") == $(< "${checksum_file}") ]]
}

record_target_checksum() {
  local target=$1
  local checksum_file
  checksum_file=$(target_checksum_file "${target}")
  cksum < "${target}" > "${checksum_file}.tmp"
  mv "${checksum_file}.tmp" "${checksum_file}"
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}" "${TARGET_CHECKSUM_DIR}"
acquire_patch_lock

if [[ ${IF_TARGET_CHANGED} == true ]] && target_unchanged "${TARGET}"; then
  echo "target unchanged; skipping patch reapply and service restart"
  exit 0
fi

if [[ ! -f ${TARGET} ]]; then
  echo "missing target: ${TARGET}" >&2
  write_status failed-missing-target
  exit 1
fi

patched_count=$(grep -Fc 'unmount_received_subvol' "${TARGET}" || true)
pre_unmount_count=$(grep -Fc '    $unmount_received_subvol->() if $exists;' "${TARGET}" || true)
post_unmount_count=$(grep -Fc '        $unmount_received_subvol->();' "${TARGET}" || true)
original_count=$(grep -Fc "run_command(['zfs', 'recv', '-F', '-x', 'encryption', '--', \$zfspath]" "${TARGET}" || true)
backup=

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

if ! perl -c "${TARGET}" >/dev/null; then
  if [[ -n ${backup} ]]; then
    cp "${backup}" "${TARGET}"
    echo "validation failed; restored ${TARGET} from ${backup}" >&2
  fi
  write_status failed-validation
  exit 1
fi

if [[ ${RESTART_SERVICES} == true ]]; then
  systemctl try-restart pvedaemon.service pve-ha-lrm.service pvescheduler.service
fi

record_target_checksum "${TARGET}"

# Replication and migration tasks run in forked workers, which load this module
# from disk on the next job. Package-triggered reapplies restart the relevant
# daemons so long-lived processes do not keep superseded Perl code loaded.
write_status "${state}"
cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab ZFS receive cache patch after package updates.
// Deferred via systemd-run so the reapply runs after the dpkg transaction has
// fully replaced ZFSPoolPlugin.pm and released the shared patch lock, instead
// of racing the in-transaction Post-Invoke (which could leave the file
// unpatched after a libpve-storage-perl upgrade). Falls back to inline if
// systemd-run is unavailable.
DPkg::Post-Invoke {
  "if command -v systemd-run >/dev/null 2>&1 && [ -x ${PATCH_SCRIPT} ]; then systemd-run --collect --unit=homelab-pve-zfs-recv-cache-patch --on-active=30s ${PATCH_SCRIPT} --if-target-changed --restart-services >/var/log/homelab-pve-zfs-recv-cache-patch.log 2>&1 || true; elif [ -x ${PATCH_SCRIPT} ]; then ${PATCH_SCRIPT} --if-target-changed --restart-services >/var/log/homelab-pve-zfs-recv-cache-patch.log 2>&1 || true; fi";
};
EOF
chmod 0644 "${APT_HOOK}"

"${PATCH_SCRIPT}"
