#!/usr/bin/env bash
set -euo pipefail

TARGET=/usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm
STATE_DIR=/var/lib/homelab/pve-zfs-large-block-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-large-block-patch
STATUS_FILE=${STATE_DIR}/status

ORIGINAL="    my \$cmd = ['zfs', 'send', '-RpvU'];"
PATCHED="    my \$cmd = ['zfs', 'send', '-RpvUL'];"

if [[ ${EUID} -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

if [[ ! -f ${TARGET} ]]; then
  echo "missing target: ${TARGET}" >&2
  exit 1
fi

if grep -Fq "${PATCHED}" "${TARGET}"; then
  state=patched
elif grep -Fq "${ORIGINAL}" "${TARGET}"; then
  backup="${BACKUP_DIR}/ZFSPoolPlugin.pm.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp "${TARGET}" "${backup}"
  PATCHED_LINE=${PATCHED} ORIGINAL_LINE=${ORIGINAL} perl -0pi -e 's/\Q$ENV{ORIGINAL_LINE}\E/$ENV{PATCHED_LINE}/' "${TARGET}"
  state=patched
else
  echo "unexpected ZFS send line in ${TARGET}; refusing to patch" >&2
  grep -nF "['zfs', 'send'" "${TARGET}" >&2 || true
  exit 1
fi

package_version=$(dpkg-query -W -f='${Version}' libpve-storage-perl 2>/dev/null || true)
{
  printf 'state=%s\n' "${state}"
  printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'target=%s\n' "${TARGET}"
  printf 'package=libpve-storage-perl\n'
  printf 'package_version=%s\n' "${package_version}"
  grep -nF "['zfs', 'send'" "${TARGET}"
} > "${STATUS_FILE}"

cat "${STATUS_FILE}"
