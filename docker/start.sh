#!/bin/bash
# Runs `docker compose up -d` in each subdirectory beside this script.
# Place this file in /mnt/cache/appdata and execute it.
# Supports custom startup order for dependencies.

set -u

# Base directory = directory of this script
ROOT="$(cd "$(dirname "$0")" && pwd)"

# Define startup order (stacks that need to run first)
# Add directory names here in the order they should start
ORDERED_STACKS=(
  "traefik2"
  # Add more stacks here if they have dependencies
  # Example: "redis" "database" etc.
)

# Directories to ignore (no compose.yml or should be skipped)
IGNORE_DIRS=(
  # Add directory names to skip here
  # Example: "backup" "scripts" etc.
)

# Track which stacks we've already started
declare -A started_stacks

# Build ignore lookup table
declare -A ignore_lookup
for dir in "${IGNORE_DIRS[@]}"; do
  ignore_lookup["$dir"]=1
done

echo "=== Starting Docker stacks with custom order ==="
echo ""

# Start ordered stacks first
for stack in "${ORDERED_STACKS[@]}"; do
  stack_dir="$ROOT/$stack"
  if [ -d "$stack_dir" ]; then
    if [ ! -f "$stack_dir/compose.yml" ] && [ ! -f "$stack_dir/docker-compose.yml" ]; then
      echo "!! WARNING: No compose file found in $stack_dir, skipping"
      started_stacks["$stack"]=1
      continue
    fi
    
    echo ">>> $stack_dir (priority order)"
    (
      cd "$stack_dir" && docker compose pull && docker compose up -d
    ) || {
      code=$?
      echo "!! failed in $stack_dir (exit $code)"
    }
    started_stacks["$stack"]=1
  else
    echo "!! WARNING: Ordered stack '$stack' not found at $stack_dir"
  fi
done

[ ${#ORDERED_STACKS[@]} -gt 0 ] && echo "" && echo ">>> Starting remaining stacks..." && echo ""

# Now start all other stacks
found=0
skipped=0
for d in "$ROOT"/*/; do
  [ -d "$d" ] || continue
  
  # Get just the directory name
  stack_name="$(basename "$d")"
  
  # Skip if in ignore list
  if [ "${ignore_lookup[$stack_name]:-0}" = "1" ]; then
    ((skipped++))
    continue
  fi
  
  # Skip if we already started this stack
  [ "${started_stacks[$stack_name]:-0}" = "1" ] && continue
  
  # Skip if no compose file
  if [ ! -f "$d/compose.yml" ] && [ ! -f "$d/docker-compose.yml" ]; then
    echo "!! No compose file in $d, skipping"
    ((skipped++))
    continue
  fi
  
  found=1
  echo ">>> $d"
  (
    cd "$d" && docker compose pull && docker compose up -d
  ) || {
    code=$?
    echo "!! failed in $d (exit $code)"
  }
done

echo ""
[ "$skipped" -gt 0 ] && echo "Skipped $skipped director(ies)"
[ "$found" -eq 0 ] && [ ${#ORDERED_STACKS[@]} -eq 0 ] && echo "No subdirectories found under $ROOT"
echo "=== Done ==="
