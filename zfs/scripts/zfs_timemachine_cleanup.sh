#!/bin/bash
# Cleanup timemachine ZFS snapshots - keep only the most recent
# Run after zfs_nas_replications_appdata
# syncoid strict-mirror will automatically clean destination

DATASET="cache/timemachine"

echo "Cleaning up timemachine snapshots, keeping only the most recent..."

# Get all snapshots except the last one, then destroy them
zfs list -t snapshot -o name -H "$DATASET" 2>/dev/null | head -n -1 | while read snap; do
    echo "Destroying: $snap"
    zfs destroy "$snap"
done

# Show remaining
echo "Remaining snapshots:"
zfs list -t snapshot -o name,used,refer "$DATASET"
