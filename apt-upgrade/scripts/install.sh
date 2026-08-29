#!/bin/bash
# install.sh - Install apt dist-upgrade service (and optionally a daily timer)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
FORCE_UPDATE=${FORCE_UPDATE:-false}

SERVICE_NAME="homelab-apt-dist-upgrade.service"
TIMER_NAME="homelab-apt-dist-upgrade.timer"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
TIMER_PATH="/etc/systemd/system/$TIMER_NAME"
AUTO_REBOOT_PATH="/etc/apt/apt.conf.d/53homelab-auto-reboot"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1

# Source env to get AUTOUPGRADE, SCHEDULE, PAUSED, and AUTO_REBOOT
AUTOUPGRADE="false"
SCHEDULE="*-*-* 09:00:00"
PAUSED="false"
AUTO_REBOOT="false"
if [[ -f "$BUILD_DIR/env" ]]; then
    # shellcheck source=/dev/null
    source "$BUILD_DIR/env"
fi

# Unattended reboot is delegated to unattended-upgrades, which is already
# present and timer-driven on these hosts -- this only sets the reboot keys it
# leaves unset by default. Removing the drop-in when auto_reboot is false (or
# removed from hosts.conf) is what makes the flag reversible; without it a host
# would keep rebooting itself after the flag was taken away.
apply_auto_reboot() {
    if [[ "$AUTO_REBOOT" != "true" ]]; then
        if [[ -e "$AUTO_REBOOT_PATH" ]]; then
            rm -f "$AUTO_REBOOT_PATH"
            print_sub "Removed $AUTO_REBOOT_PATH (auto_reboot disabled)"
        fi
        return 0
    fi

    if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
        print_error "auto_reboot requires unattended-upgrades, which is not installed"
        print_sub "It supplies the reboot mechanism; this module only sets its keys."
        return 1
    fi

    local rc=0
    copy_if_changed "$BUILD_DIR/auto-reboot.conf" "$AUTO_REBOOT_PATH" \
        "$(basename "$AUTO_REBOOT_PATH")" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || return "$rc"

    # Verify the resolved policy, not the file just written: APT reads the whole
    # of apt.conf.d in order, so a later fragment could still override this.
    local dump
    dump="$(apt-config dump Unattended-Upgrade::Automatic-Reboot 2>/dev/null || true)"
    if ! printf '%s' "$dump" | grep -q 'Automatic-Reboot "true"'; then
        print_error "resolved Unattended-Upgrade::Automatic-Reboot is not true"
        printf '%s\n' "$dump" >&2
        return 1
    fi

    # The reboot only ever happens at the end of an unattended-upgrades run, so
    # the timer that invokes it is a hard dependency of this feature.
    if ! systemctl is-enabled --quiet apt-daily-upgrade.timer 2>/dev/null; then
        print_error "apt-daily-upgrade.timer is not enabled; auto_reboot would never fire"
        return 1
    fi

    print_ok "Unattended-upgrades will reboot when /var/run/reboot-required is present"
    return 0
}

# Always install the service unit (used both on-demand and by the timer)
local_changed=false
rc=0
copy_if_changed "$BUILD_DIR/service" "$SERVICE_PATH" "$SERVICE_NAME" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
[[ $rc -eq 0 ]] && local_changed=true

if [[ "$local_changed" == true ]]; then
    systemctl daemon-reload
fi

# Paused: keep the service unit installed but stop/disable the timer and skip
# any on-demand upgrade. Flip apt-upgrade.paused back to false to resume.
pause_rc=0
homelab_apply_pause "$PAUSED" "$TIMER_NAME" || pause_rc=$?
if [[ $pause_rc -eq 0 ]]; then
    # Pause means the host stops acting on its own, so it must also stop
    # rebooting on its own -- drop the reboot keys and let unattended-upgrades
    # fall back to its default of false. Resuming redeploys them.
    AUTO_REBOOT="false"
    apply_auto_reboot || exit 1
    print_header "apt-upgrade paused"
    if [[ -f /var/run/reboot-required ]]; then
        print_warn "Reboot required on $(hostname)"
    fi
    exit 0
fi
[[ $pause_rc -eq 1 ]] || exit "$pause_rc"

# Applied before the upgrade runs, so a dist-upgrade started below that installs
# a kernel already has the reboot policy in place.
apply_auto_reboot || exit 1

if [[ "$AUTOUPGRADE" == "true" ]]; then
    # Install and enable the daily timer
    rc=0
    copy_if_changed "$BUILD_DIR/timer" "$TIMER_PATH" "$TIMER_NAME" || rc=$?
    [[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"
    [[ $rc -eq 0 ]] && local_changed=true

    if [[ "$local_changed" == true ]]; then
        systemctl daemon-reload
    fi

    if ! systemctl is-enabled --quiet "$TIMER_NAME" 2>/dev/null; then
        systemctl enable --now "$TIMER_NAME" >/dev/null
        print_sub "Timer enabled at $SCHEDULE"
    else
        if [[ "$local_changed" == true ]]; then
            systemctl restart "$TIMER_NAME" >/dev/null
            print_sub "Timer restarted"
        else
            print_sub "Timer already enabled"
        fi
    fi

    systemctl list-timers --all --no-pager | grep -F "$TIMER_NAME" || true
else
    # Autoupgrade not enabled: retire any previous timer, then run once now.
    # Status is intentionally discarded: no previous timer is the normal case.
    retire_systemd_unit "$TIMER_NAME" "$TIMER_PATH" || true
    print_sub "Running apt upgrade now (autoupgrade not enabled)..."
    systemctl start "$SERVICE_NAME"
    print_sub "Upgrade complete"
fi

# Reboot check
if [[ -f /var/run/reboot-required ]]; then
    if [[ "$AUTO_REBOOT" == "true" ]]; then
        print_sub "Reboot required on $(hostname); unattended-upgrades will take it"
    else
        print_warn "Reboot required on $(hostname)"
    fi
fi
