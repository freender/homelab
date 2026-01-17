#!/bin/bash
# Backup unRAID Flash Drive OS Files to NAS Backup Directory
# This script uses rsync to backup OS files from the unRAID flash drive (assumed to be mounted at /boot)
# to a backup directory (/mnt/user/backup/nas-flash).
#
# This version is designed to run from the User Scripts plugin in unRAID,
# excludes the .git directory, and does not log to a file.
#
# Additional Exclusions:
# You can add other non-essential files or directories (like temporary files or caches) by appending extra --exclude options.

# Define source and destination directories
SRC="/boot"
DEST="/mnt/cache/tower/boot"

echo "Starting backup from $SRC to $DEST..."

# Check if destination directory exists; if not, attempt to create it
if [ ! -d "$DEST" ]; then
    echo "Destination directory $DEST does not exist. Creating it..."
    mkdir -p "$DEST"
    if [ $? -ne 0 ]; then
        echo "Error: Unable to create backup directory $DEST. Exiting."
        exit 1
    fi
fi

# Define rsync exclude patterns
# Exclude the .git directory (note the trailing slash to denote a directory)
EXCLUDES="--exclude=.git/"
# You can add additional excludes here, e.g.,
# EXCLUDES="$EXCLUDES --exclude=*.tmp --exclude=cache/"

# Run rsync command:
# -a: Archive mode (preserves symbolic links, permissions, timestamps, etc.)
# -v: Verbose output
# -h: Human-readable numbers
# --delete: Remove files from the destination that no longer exist in the source
rsync -avh --delete --chown=99:100 $EXCLUDES "$SRC/" "$DEST/"

echo "Backup completed."