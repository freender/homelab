#!/usr/bin/env bash
set -euo pipefail

STATE_DIR=/var/lib/homelab/pve-lxc-pre-replication-patch
BACKUP_DIR=/var/backups/homelab/pve-lxc-pre-replication-patch
PATCH_SCRIPT=/usr/local/sbin/homelab-pve-lxc-pre-replication-patch
APT_HOOK=/etc/apt/apt.conf.d/99-homelab-pve-lxc-pre-replication-patch

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

LXC_MIGRATE_TARGET=/usr/share/perl5/PVE/LXC/Migrate.pm
HA_PVECT_TARGET=/usr/share/perl5/PVE/HA/Resources/PVECT.pm
STATE_DIR=/var/lib/homelab/pve-lxc-pre-replication-patch
BACKUP_DIR=/var/backups/homelab/pve-lxc-pre-replication-patch
STATUS_FILE=${STATE_DIR}/status
LOCK_FILE=/run/lock/homelab-pve-patches.lock

LXC_ORIGINAL=$(cat <<'EOF'
    # in restart mode, we shutdown the container before migrating
    if ($restart && $running) {
        my $timeout = $self->{opts}->{timeout} // 180;

        $self->log('info', "shutdown CT $vmid\n");

        PVE::LXC::vm_stop($vmid, 0, $timeout);

        $running = 0;
    }
EOF
)

LXC_PATCHED=$(cat <<'EOF'
    if ($restart && $running && !$remote) {
        my $rep_cfg = PVE::ReplicationConfig->new();
        if (my $jobcfg = $rep_cfg->find_local_replication_job($vmid, $self->{node})) {
            my $start_time = time();
            my $logfunc = sub { my ($msg) = @_; $self->log('info', "pre-stop replication: $msg"); };
            $self->log('info', 'run pre-stop replication before shutdown');
            PVE::Replication::run_replication(
                'PVE::LXC::Config', $jobcfg, $start_time, $start_time, $logfunc,
            );
        }
    }

    # in restart mode, we shutdown the container before migrating
    if ($restart && $running) {
        my $timeout = $self->{opts}->{timeout} // 180;

        $self->log('info', "shutdown CT $vmid\n");

        PVE::LXC::vm_stop($vmid, 0, $timeout);

        $running = 0;
    }
EOF
)

HA_ORIGINAL=$(cat <<'EOF'
    my $params = {
        node => $nodename,
        vmid => $id,
        target => $target,
        online => 0, # we cannot migrate CT (yet) online, only relocate
    };

    # always relocate container for now
    if ($class->check_running($haenv, $id)) {
        $class->shutdown($haenv, $id);
    }
EOF
)

HA_PATCHED=$(cat <<'EOF'
    my $params = {
        node => $nodename,
        vmid => $id,
        target => $target,
        online => 0, # we cannot migrate CT (yet) online, only relocate
        restart => 1,
    };

    # Let the LXC restart migration worker stop/start running containers so it
    # can run pre-stop replication before shutdown.
EOF
)

write_status() {
  local state=$1
  local pve_container_version
  local pve_ha_manager_version
  pve_container_version=$(dpkg-query -W -f='${Version}' pve-container 2>/dev/null || true)
  pve_ha_manager_version=$(dpkg-query -W -f='${Version}' pve-ha-manager 2>/dev/null || true)
  {
    printf 'state=%s\n' "${state}"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'lxc_migrate_target=%s\n' "${LXC_MIGRATE_TARGET}"
    printf 'ha_pvect_target=%s\n' "${HA_PVECT_TARGET}"
    printf 'pve_container_version=%s\n' "${pve_container_version}"
    printf 'pve_ha_manager_version=%s\n' "${pve_ha_manager_version}"
    grep -nF 'pre-stop replication before shutdown' "${LXC_MIGRATE_TARGET}" || true
    grep -nF 'restart => 1' "${HA_PVECT_TARGET}" || true
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

patch_target() {
  local target=$1
  local original=$2
  local patched=$3
  local marker=$4
  local label=$5
  local timestamp
  local patched_count
  local original_count

  if [[ ! -f ${target} ]]; then
    echo "missing target: ${target}" >&2
    write_status "failed-missing-${label}"
    exit 1
  fi

  patched_count=$(grep -Fc "${marker}" "${target}" || true)
  original_count=$(ORIGINAL_BLOCK=${original} perl -0ne 'my $count = () = /\Q$ENV{ORIGINAL_BLOCK}\E/g; print $count' "${target}")

  if [[ ${patched_count} -eq 1 ]]; then
    printf '%s=already-patched\n' "${label}"
    return
  fi

  if [[ ${patched_count} -eq 0 && ${original_count} -eq 1 ]]; then
    timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    cp "${target}" "${BACKUP_DIR}/$(basename "${target}").${timestamp}.bak"
    PATCHED_BLOCK=${patched} ORIGINAL_BLOCK=${original} \
      perl -0pi -e 's/\Q$ENV{ORIGINAL_BLOCK}\E/$ENV{PATCHED_BLOCK}/' "${target}"
    printf '%s=patched\n' "${label}"
    return
  fi

  echo "unexpected ${label} migration stanza in ${target}; refusing to patch" >&2
  grep -nF "${marker}" "${target}" >&2 || true
  write_status "failed-unexpected-${label}"
  exit 1
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"
acquire_patch_lock

tmp_status=$(mktemp)
patch_target "${LXC_MIGRATE_TARGET}" "${LXC_ORIGINAL}" "${LXC_PATCHED}" \
  'pre-stop replication before shutdown' 'lxc_migrate' | tee -a "${tmp_status}"
patch_target "${HA_PVECT_TARGET}" "${HA_ORIGINAL}" "${HA_PATCHED}" \
  'can run pre-stop replication before shutdown' 'ha_pvect' | tee -a "${tmp_status}"

perl -c "${LXC_MIGRATE_TARGET}" >/dev/null
perl -c "${HA_PVECT_TARGET}" >/dev/null

if [[ ${RESTART_SERVICES} == true ]]; then
  systemctl try-restart pvedaemon.service pve-ha-lrm.service
fi

if grep -Fq '=patched' "${tmp_status}"; then
  state=patched
else
  state=already-patched
fi
rm -f "${tmp_status}"

write_status "${state}"
cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab LXC pre-replication migration patch after package updates.
// Deferred via systemd-run so the reapply runs after the dpkg transaction has
// fully replaced the target Perl module and released the shared patch lock,
// instead of racing the in-transaction Post-Invoke (which could leave the file
// unpatched after a package upgrade). Falls back to inline if systemd-run is
// unavailable.
DPkg::Post-Invoke {
  "if command -v systemd-run >/dev/null 2>&1 && [ -x ${PATCH_SCRIPT} ]; then systemd-run --collect --unit=homelab-pve-lxc-pre-replication-patch --on-active=30s ${PATCH_SCRIPT} --restart-services >/var/log/homelab-pve-lxc-pre-replication-patch.log 2>&1 || true; elif [ -x ${PATCH_SCRIPT} ]; then ${PATCH_SCRIPT} --restart-services >/var/log/homelab-pve-lxc-pre-replication-patch.log 2>&1 || true; fi";
};
EOF
chmod 0644 "${APT_HOOK}"

"${PATCH_SCRIPT}" --restart-services
