#!/usr/bin/env bash
set -euo pipefail

STATE_DIR=/var/lib/homelab/pve-ha-fast-migrate-patch
BACKUP_DIR=/var/backups/homelab/pve-ha-fast-migrate-patch
PATCH_SCRIPT=/usr/local/sbin/homelab-pve-ha-fast-migrate-patch
APT_HOOK=/etc/apt/apt.conf.d/99-homelab-pve-ha-fast-migrate-patch

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

HA_ENV_TARGET=/usr/share/perl5/PVE/HA/Env.pm
HA_PVE2_TARGET=/usr/share/perl5/PVE/HA/Env/PVE2.pm
HA_LRM_TARGET=/usr/share/perl5/PVE/HA/LRM.pm
HA_CRM_TARGET=/usr/share/perl5/PVE/HA/CRM.pm
STATE_DIR=/var/lib/homelab/pve-ha-fast-migrate-patch
BACKUP_DIR=/var/backups/homelab/pve-ha-fast-migrate-patch
TARGET_CHECKSUM_DIR=/var/lib/homelab/pve-patches/target-checksums/pve-ha-fast-migrate-patch
STATUS_FILE=${STATE_DIR}/status
LOCK_FILE=/run/lock/homelab-pve-patches.lock

ENV_ORIGINAL=$(cat <<'EOF'
sub sleep_until {
    my ($self, $end_time) = @_;

    return $self->{plug}->sleep_until($end_time);
}
EOF
)

ENV_PATCHED=$(cat <<'EOF'
sub sleep_until {
    my ($self, $end_time, $wake) = @_;

    return $self->{plug}->sleep_until($end_time, $wake);
}

# homelab-ha-fast-migrate: only the PVE2 environment can report the manager status version
sub manager_status_version {
    my ($self, $refresh) = @_;

    return undef if !$self->{plug}->can('manager_status_version');

    return $self->{plug}->manager_status_version($refresh);
}
EOF
)

PVE2_ORIGINAL=$(cat <<'EOF'
sub sleep_until {
    my ($self, $end_time) = @_;

    for (;;) {
        my $cur_time = time();

        last if $cur_time >= $end_time;

        $self->sleep(1);
    }
}
EOF
)

PVE2_PATCHED=$(cat <<'EOF'
sub sleep_until {
    my ($self, $end_time, $wake) = @_;

    for (;;) {
        my $cur_time = time();

        last if $cur_time >= $end_time;

        $self->sleep(1);

        # homelab-ha-fast-migrate: optionally end the wait early for a pending HA migration
        next if !$wake;
        my $wake_now = eval { $wake->() };
        if (my $err = $@) {
            $self->log('err', "early wake-up check failed - $err");
            $wake = undef;
        }
        last if $wake_now;
    }
}

sub manager_status_version {
    my ($self, $refresh) = @_;

    PVE::Cluster::cfs_update() if $refresh;

    return PVE::Cluster::cfs_file_version('ha/manager_status');
}
EOF
)

LRM_ORIGINAL=$(cat <<'EOF'
        $self->update_lrm_status();

        return 0 if $shutdown;

        $haenv->sleep_until($startime + $max_time);

    } elsif ($state eq 'lost_agent_lock') {
EOF
)

LRM_PATCHED=$(cat <<'EOF'
        $self->update_lrm_status();

        return 0 if $shutdown;

        # homelab-ha-fast-migrate: end the LRM round early while an HA migration is handed over
        my $wake = eval {
            my $nodename = $haenv->nodename();
            my $workers = $self->{workers};
            my @outgoing = grep {
                $workers->{$_}->{pid} && ($workers->{$_}->{state} // '') =~ m/^(?:migrate|relocate)$/
            } keys %$workers;
            my $ss = $self->{service_status} // {};
            my %incoming = map { $_ => $ss->{$_}->{uid} // '' } grep {
                ($ss->{$_}->{state} // '') =~ m/^(?:migrate|relocate)$/
                    && ($ss->{$_}->{target} // '') eq $nodename
            } keys %$ss;
            my $ms_version = %incoming ? $haenv->manager_status_version() : undef;
            return undef if !@outgoing && !defined($ms_version);
            return sub {
                if (@outgoing) {
                    $self->check_active_workers();
                    if (my @done = grep { !exists($workers->{$_}) } @outgoing) {
                        $haenv->log('info', "migration of '@done' finished, ending LRM round early");
                        return 1;
                    }
                }
                if (defined($ms_version)) {
                    my $version = $haenv->manager_status_version(1);
                    if (defined($version) && $version != $ms_version) {
                        $ms_version = $version;
                        my $new_ss = $haenv->read_manager_status()->{service_status} // {};
                        for my $sid (sort keys %incoming) {
                            my $uid = $new_ss->{$sid} ? $new_ss->{$sid}->{uid} // '' : '';
                            next if $uid eq $incoming{$sid};
                            $haenv->log('info', "service '$sid' handed over, ending LRM round early");
                            return 1;
                        }
                    }
                }
                return 0;
            };
        };
        $haenv->log('err', "migration wake-up setup failed - $@") if $@;

        $haenv->sleep_until($startime + $max_time, $wake);

    } elsif ($state eq 'lost_agent_lock') {
EOF
)

CRM_ORIGINAL=$(cat <<'EOF'
        return 0 if $shutdown;

        $haenv->sleep_until($startime + $max_time);

    } elsif ($state eq 'lost_manager_lock') {
EOF
)

CRM_PATCHED=$(cat <<'EOF'
        return 0 if $shutdown;

        # homelab-ha-fast-migrate: end the CRM round early once the source LRM reported a migration
        my $wake = eval {
            my $ss = $manager->{ss} // {};
            my $has_result = sub {
                my ($node, $uid) = @_;
                my $lrm_status = eval { $haenv->read_lrm_status($node) } // {};
                my $results = $lrm_status->{results};
                return ref($results) eq 'HASH' && defined($results->{$uid});
            };
            my %pending;
            for my $sid (keys %$ss) {
                my $sd = $ss->{$sid};
                next if ($sd->{state} // '') !~ m/^(?:migrate|relocate)$/;
                next if !$sd->{node} || !$sd->{uid};
                # a result that is already there gets processed by the regular next round
                next if $has_result->($sd->{node}, $sd->{uid});
                $pending{$sid} = [$sd->{node}, $sd->{uid}];
            }
            return undef if !%pending;
            return sub {
                for my $sid (sort keys %pending) {
                    next if !$has_result->($pending{$sid}->@*);
                    $haenv->log('info', "LRM reported result for '$sid', ending CRM round early");
                    return 1;
                }
                return 0;
            };
        };
        $haenv->log('err', "migration wake-up setup failed - $@") if $@;

        $haenv->sleep_until($startime + $max_time, $wake);

    } elsif ($state eq 'lost_manager_lock') {
EOF
)

write_status() {
  local state=$1
  local pve_ha_manager_version
  pve_ha_manager_version=$(dpkg-query -W -f='${Version}' pve-ha-manager 2>/dev/null || true)
  {
    printf 'state=%s\n' "${state}"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'pve_ha_manager_version=%s\n' "${pve_ha_manager_version}"
    for target in "${HA_ENV_TARGET}" "${HA_PVE2_TARGET}" "${HA_LRM_TARGET}" "${HA_CRM_TARGET}"; do
      printf 'target=%s\n' "${target}"
      grep -nF 'homelab-ha-fast-migrate:' "${target}" || true
    done
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

all_targets_unchanged() {
  target_unchanged "${HA_ENV_TARGET}" && target_unchanged "${HA_PVE2_TARGET}" \
    && target_unchanged "${HA_LRM_TARGET}" && target_unchanged "${HA_CRM_TARGET}"
}

record_target_checksum() {
  local target=$1
  local checksum_file
  checksum_file=$(target_checksum_file "${target}")
  cksum < "${target}" > "${checksum_file}.tmp"
  mv "${checksum_file}.tmp" "${checksum_file}"
}

count_block() {
  BLOCK=$1 perl -0ne 'my $count = () = /\Q$ENV{BLOCK}\E/g; print $count' "$2"
}

# All four targets are checked before any is touched: the patched LRM/CRM call
# methods that only the patched Env.pm/PVE2.pm provide, so a half-applied patch
# would break the HA daemons instead of leaving them stock.
check_target() {
  local target=$1
  local original=$2
  local marker=$3
  local label=$4
  local patched_count
  local original_count

  if [[ ! -f ${target} ]]; then
    echo "missing target: ${target}" >&2
    write_status "failed-missing-${label}"
    exit 1
  fi

  patched_count=$(grep -Fc "${marker}" "${target}" || true)
  original_count=$(count_block "${original}" "${target}")

  if [[ ${patched_count} -eq 1 ]]; then
    echo already-patched
    return
  fi
  if [[ ${patched_count} -eq 0 && ${original_count} -eq 1 ]]; then
    echo original
    return
  fi

  echo "unexpected ${label} stanza in ${target}; refusing to patch" >&2
  grep -nF "${marker}" "${target}" >&2 || true
  write_status "failed-unexpected-${label}"
  exit 1
}

replace_block() {
  local target=$1
  local from=$2
  local to=$3
  local backup
  backup="${BACKUP_DIR}/$(basename "${target}").${TIMESTAMP}.bak"
  cp "${target}" "${backup}"
  BACKUPS+=("${backup}|${target}")
  TO_BLOCK=${to} FROM_BLOCK=${from} \
    perl -0pi -e 's/\Q$ENV{FROM_BLOCK}\E/$ENV{TO_BLOCK}/' "${target}"
}

restore_backups() {
  local entry
  for entry in "${BACKUPS[@]}"; do
    cp "${entry%%|*}" "${entry##*|}"
  done
}

# Load the modules the way pve-ha-lrm/pve-ha-crm do: `perl -c` by path fails
# even on stock PVE2.pm (it reaches itself again through PVE::API2::Qemu), and
# loading also catches a patched call into a method the other file should add.
validate_targets() {
  perl -e 'use PVE::HA::Env; use PVE::HA::Env::PVE2; use PVE::HA::LRM; use PVE::HA::CRM;
    PVE::HA::Env::PVE2->can("manager_status_version") or die "PVE2 lacks manager_status_version\n";
    PVE::HA::Env->can("manager_status_version") or die "Env lacks manager_status_version\n";'
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}" "${TARGET_CHECKSUM_DIR}"
acquire_patch_lock

if [[ ${IF_TARGET_CHANGED} == true ]] && all_targets_unchanged; then
  echo "targets unchanged; skipping patch reapply and service restart"
  exit 0
fi

env_state=$(check_target "${HA_ENV_TARGET}" "${ENV_ORIGINAL}" \
  'homelab-ha-fast-migrate: only the PVE2 environment' 'ha_env')
pve2_state=$(check_target "${HA_PVE2_TARGET}" "${PVE2_ORIGINAL}" \
  'homelab-ha-fast-migrate: optionally end the wait early' 'ha_pve2')
lrm_state=$(check_target "${HA_LRM_TARGET}" "${LRM_ORIGINAL}" \
  'homelab-ha-fast-migrate: end the LRM round early' 'ha_lrm')
crm_state=$(check_target "${HA_CRM_TARGET}" "${CRM_ORIGINAL}" \
  'homelab-ha-fast-migrate: end the CRM round early' 'ha_crm')

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUPS=()
changed=false
if [[ ${env_state} == original ]]; then
  replace_block "${HA_ENV_TARGET}" "${ENV_ORIGINAL}" "${ENV_PATCHED}"
  changed=true
fi
if [[ ${pve2_state} == original ]]; then
  replace_block "${HA_PVE2_TARGET}" "${PVE2_ORIGINAL}" "${PVE2_PATCHED}"
  changed=true
fi
if [[ ${lrm_state} == original ]]; then
  replace_block "${HA_LRM_TARGET}" "${LRM_ORIGINAL}" "${LRM_PATCHED}"
  changed=true
fi
if [[ ${crm_state} == original ]]; then
  replace_block "${HA_CRM_TARGET}" "${CRM_ORIGINAL}" "${CRM_PATCHED}"
  changed=true
fi
printf 'ha_env=%s\nha_pve2=%s\nha_lrm=%s\nha_crm=%s\n' \
  "${env_state}" "${pve2_state}" "${lrm_state}" "${crm_state}"

if ! validate_targets; then
  restore_backups
  echo "validation failed; restored all patched targets from ${BACKUP_DIR}" >&2
  write_status "failed-validation"
  exit 1
fi

if [[ ${RESTART_SERVICES} == true ]]; then
  systemctl try-restart pve-ha-lrm.service pve-ha-crm.service
fi

if [[ ${changed} == true ]]; then
  state=patched
else
  state=already-patched
fi

record_target_checksum "${HA_ENV_TARGET}"
record_target_checksum "${HA_PVE2_TARGET}"
record_target_checksum "${HA_LRM_TARGET}"
record_target_checksum "${HA_CRM_TARGET}"
write_status "${state}"
cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab HA fast-migrate patch after package updates.
// Deferred via systemd-run so the reapply runs after the dpkg transaction has
// fully replaced the target Perl modules and released the shared patch lock.
// Falls back to inline if systemd-run is unavailable.
DPkg::Post-Invoke {
  "if command -v systemd-run >/dev/null 2>&1 && [ -x ${PATCH_SCRIPT} ]; then systemd-run --collect --unit=homelab-pve-ha-fast-migrate-patch --on-active=30s ${PATCH_SCRIPT} --if-target-changed --restart-services >/var/log/homelab-pve-ha-fast-migrate-patch.log 2>&1 || true; elif [ -x ${PATCH_SCRIPT} ]; then ${PATCH_SCRIPT} --if-target-changed --restart-services >/var/log/homelab-pve-ha-fast-migrate-patch.log 2>&1 || true; fi";
};
EOF
chmod 0644 "${APT_HOOK}"

"${PATCH_SCRIPT}" --restart-services
