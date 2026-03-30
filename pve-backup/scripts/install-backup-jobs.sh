#!/bin/bash
# install-backup-jobs.sh - Install PVE standalone backup jobs

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
PLAN_FILE="$BUILD_DIR/jobs-plan.conf"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_file "$PLAN_FILE" "$PLAN_FILE" || exit 1

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
    local exclude="$3"
    local jobs_json

    jobs_json=$(pvesh get /cluster/backup --output-format json 2>/dev/null || printf '[]')
    JOBS_JSON="$jobs_json" python3 -c '
import json
import os
import sys

storage = sys.argv[1]
vmid = sys.argv[2]
exclude = sys.argv[3]

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
    current_exclude = str(job.get("exclude", ""))
    if vmid:
        if current_vmid == vmid and current_exclude == exclude:
            print(job.get("id", ""))
            break
    else:
        if str(job.get("all", 0)) == "1" and current_exclude == exclude:
            print(job.get("id", ""))
            break
else:
    print("")
' "$storage" "$vmid" "$exclude"
}

for (( i=0; i<JOB_COUNT; i++ )); do
    schedule_var="JOB_${i}_SCHEDULE"
    storage_var="JOB_${i}_STORAGE"
    vmid_var="JOB_${i}_VMID"
    exclude_var="JOB_${i}_EXCLUDE"
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
    exclude="${!exclude_var}"
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

    if [[ -n "$vmid" && -n "$exclude" ]]; then
        print_error "Job $i cannot set both vmid and exclude"
        exit 1
    fi

    job_id=$(find_job_id "$storage" "$vmid" "$exclude")

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
            pvesh set "/cluster/backup/$job_id" --delete exclude >/dev/null
        else
            pvesh set "/cluster/backup/$job_id" --all 1
            pvesh set "/cluster/backup/$job_id" --delete vmid >/dev/null
            if [[ -n "$exclude" ]]; then
                pvesh set "/cluster/backup/$job_id" --exclude "$exclude"
            else
                pvesh set "/cluster/backup/$job_id" --delete exclude >/dev/null
            fi
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
            if [[ -n "$exclude" ]]; then
                pvesh create /cluster/backup \
                    --schedule "$schedule" \
                    --storage "$storage" \
                    --all 1 \
                    --exclude "$exclude" \
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
    fi
done
