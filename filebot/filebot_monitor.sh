#!/bin/bash
#############################################
# FileBot Import Monitor
# 
# Purpose: Alerts if video files sit in FileBot 
#          folder without being imported
#
# Schedule: Run every 5 minutes (User Scripts)
#############################################

WORK_DIR="/mnt/user/media/downloads/filebot"
STATE_FILE="${WORK_DIR}/.filebot_tracking.state"
LOG_FILE="${WORK_DIR}/.filebot_monitor.log"

ALERT_AGE_MINUTES=15
ALERT_THRESHOLD_SECONDS=900
PURGE_DAYS=30

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

touch "$STATE_FILE"
cd "$WORK_DIR" || { log "ERROR: Cannot access $WORK_DIR"; exit 1; }

log "Starting import check..."

# Find video files older than alert threshold
find . -maxdepth 1 \( -name '*.mkv' -o -name '*.mp4' \) -mmin +${ALERT_AGE_MINUTES} 2>/dev/null | while IFS= read -r file; do
    base_file=$(basename "$file")
    
    # Check if already tracked
    if ! grep -q "|${base_file}|" "$STATE_FILE" 2>/dev/null; then
        echo "$(date +%s)|${base_file}|new" >> "$STATE_FILE"
        log "TRACKING: ${base_file}"
    fi
done

# Process tracked files
current_time=$(date +%s)
temp_state=$(mktemp)

while IFS='|' read -r timestamp filename status; do
    [[ -z "$timestamp" ]] && continue
    
    file_age=$((current_time - timestamp))
    
    # File imported (no longer exists)
    if [[ ! -f "${WORK_DIR}/${filename}" ]]; then
        log "SUCCESS: ${filename} (imported)"
        continue
    fi
    
    case "$status" in
        new)
            if [[ $file_age -ge $ALERT_THRESHOLD_SECONDS ]]; then
                /usr/local/emhttp/webGui/scripts/notify \
                    -s "FileBot Import Failed" \
                    -d "File not imported after 15 minutes: ${filename}" \
                    -i "warning"
                log "ALERT SENT: ${filename}"
                echo "${timestamp}|${filename}|alerted" >> "$temp_state"
            else
                echo "${timestamp}|${filename}|${status}" >> "$temp_state"
            fi
            ;;
        alerted)
            purge_threshold=$((PURGE_DAYS * 86400))
            if [[ $file_age -lt $purge_threshold ]]; then
                echo "${timestamp}|${filename}|${status}" >> "$temp_state"
            else
                log "PURGED: ${filename}"
            fi
            ;;
    esac
done < "$STATE_FILE"

mv "$temp_state" "$STATE_FILE"
log "Check complete"

# Trim log
if [[ -f "$LOG_FILE" ]] && [[ $(wc -l < "$LOG_FILE") -gt 1000 ]]; then
    tail -n 1000 "$LOG_FILE" > "${LOG_FILE}.tmp"
    mv "${LOG_FILE}.tmp" "$LOG_FILE"
fi
