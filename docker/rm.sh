#!/bin/sh
# Runs `docker compose down` in each subdirectory beside this script.
# Place this file in /mnt/cache/appdata and execute it.

set -u

# Base directory = directory of this script
ROOT="$(cd "$(dirname "$0")" && pwd)"

# Ask for confirmation
printf "This will run 'docker compose down --remove-orphans' in all subdirectories of %s\n" "$ROOT"
printf "Are you sure you want to continue? (yes/no): "
read -r response

case "$response" in
  yes|YES|y|Y)
    echo "Proceeding..."
    ;;
  *)
    echo "Aborted."
    exit 0
    ;;
esac

found=0
for d in "$ROOT"/*/; do
  [ -d "$d" ] || continue
  found=1
  echo ">>> $d"
  (
    cd "$d" && docker compose down --remove-orphans
  ) || {
    code=$?
    echo "!! failed in $d (exit $code)"
  }
done

[ "$found" -eq 0 ] && echo "No subdirectories found under $ROOT"
echo "Done."
