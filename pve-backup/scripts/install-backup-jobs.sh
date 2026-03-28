#!/bin/bash
# install-backup-jobs.sh - Install PVE standalone backup jobs

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
PLAN_FILE="$BUILD_DIR/jobs-plan.conf"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    print_sub() { echo "    $*"; }
    print_warn() { echo "    ✗ Warning: $*"; }
    print_error() { echo "    ✗ Error: $*" >&2; }
fi

if [[ ! -f "$PLAN_FILE" ]]; then
    print_error "Missing backup jobs plan: $PLAN_FILE"
    exit 1
fi

# shellcheck disable=SC1090
source "$PLAN_FILE"

if [[ -z "${JOB_COUNT:-}" ]]; then
    print_error "JOB_COUNT missing in $PLAN_FILE"
    exit 1
fi

if ! command -v pvesh >/dev/null 2>&1; then
    print_error "pvesh command not found"
    exit 1
fi

find_job_id() {
    local storage="$1"
    local vmid="$2"
    local jobs_json

    jobs_json=$(pvesh get /cluster/backup --output-format json 2>/dev/null || printf '[]')
    JOBS_JSON="$jobs_json" python3 -c '
import json
import os
import sys

storage = sys.argv[1]
vmid = sys.argv[2]

try:
    jobs = json.loads(os.environ.get("JOBS_JSON", "[]"))
except Exception:
    print("")
    raise SystemExit(0)

for job in jobs:
    if job.get("type") != "vzdump":
        continue
    if job.get("storage") != storage:
        continue

    current_vmid = str(job.get("vmid", ""))
    if vmid:
        if current_vmid == vmid:
            print(job.get("id", ""))
            break
    else:
        if str(job.get("all", 0)) == "1":
            print(job.get("id", ""))
            break
else:
    print("")
' "$storage" "$vmid"
}

for (( i=0; i<JOB_COUNT; i++ )); do
    schedule_var="JOB_${i}_SCHEDULE"
    storage_var="JOB_${i}_STORAGE"
    vmid_var="JOB_${i}_VMID"
    compress_var="JOB_${i}_COMPRESS"
    mode_var="JOB_${i}_MODE"
    notes_template_var="JOB_${i}_NOTES_TEMPLATE"
    notification_mode_var="JOB_${i}_NOTIFICATION_MODE"
    prune_backups_var="JOB_${i}_PRUNE_BACKUPS"
    enabled_var="JOB_${i}_ENABLED"
    fleecing_var="JOB_${i}_FLEECING"

    schedule="${!schedule_var}"
    storage="${!storage_var}"
    vmid="${!vmid_var}"
    compress="${!compress_var}"
    mode="${!mode_var}"
    notes_template="${!notes_template_var}"
    notification_mode="${!notification_mode_var}"
    prune_backups="${!prune_backups_var}"
    enabled="${!enabled_var}"
    fleecing="${!fleecing_var}"

    if [[ -z "$schedule" || -z "$storage" ]]; then
        print_error "Job $i is missing required schedule/storage"
        exit 1
    fi

    job_id=$(find_job_id "$storage" "$vmid")

    if [[ -n "$job_id" ]]; then
        print_sub "Updating backup job $job_id (storage=$storage vmid=${vmid:-all})..."
        pvesh set "/cluster/backup/$job_id" \
            --schedule "$schedule" \
            --storage "$storage" \
            --compress "$compress" \
            --mode "$mode" \
            --notes-template "$notes_template" \
            --notification-mode "$notification_mode" \
            --prune-backups "$prune_backups" \
            --enabled "$enabled" \
            --fleecing "$fleecing"

        if [[ -n "$vmid" ]]; then
            pvesh set "/cluster/backup/$job_id" --vmid "$vmid"
        else
            pvesh set "/cluster/backup/$job_id" --all 1
        fi
    else
        print_sub "Creating backup job (storage=$storage vmid=${vmid:-all})..."
        if [[ -n "$vmid" ]]; then
            pvesh create /cluster/backup \
                --schedule "$schedule" \
                --storage "$storage" \
                --vmid "$vmid" \
                --compress "$compress" \
                --mode "$mode" \
                --notes-template "$notes_template" \
                --notification-mode "$notification_mode" \
                --prune-backups "$prune_backups" \
                --enabled "$enabled" \
                --fleecing "$fleecing"
        else
            pvesh create /cluster/backup \
                --schedule "$schedule" \
                --storage "$storage" \
                --all 1 \
                --compress "$compress" \
                --mode "$mode" \
                --notes-template "$notes_template" \
                --notification-mode "$notification_mode" \
                --prune-backups "$prune_backups" \
                --enabled "$enabled" \
                --fleecing "$fleecing"
        fi
    fi
done
