#!/bin/bash
# install.sh - Sync repo-managed Docker Compose stacks into the appdata root and
# apply the ones whose definition actually changed.
#
# Stack name == directory name, both in the repo and on the host, so the compose
# project name stays identical to what start.sh already creates.
#
# This module owns compose.yml and nothing else. Runtime .env files stay host-local
# and are never read for their values, written, or removed.
#
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
# Assembled locally: shared templates rendered for this host plus verbatim
# per-host compose files, flattened into one tree. Which source a stack came
# from is settled before staging and is not this script's concern.
STACKS_DIR="$BUILD_DIR/stacks"
FORCE_UPDATE=${FORCE_UPDATE:-false}

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_file "$BUILD_DIR/env" "$BUILD_DIR/env" || exit 1

# shellcheck source=/dev/null
source "$BUILD_DIR/env"

# An empty flag would silently downgrade this to a file copy with no reconcile,
# which looks like a successful deploy while leaving containers on the old
# definition. Refuse to run on a truncated env instead.
require_env APPDATA_ROOT || exit 1
require_env APPLY_CHANGED || exit 1

require_dir "$STACKS_DIR" "$STACKS_DIR" || exit 1
require_dir "$APPDATA_ROOT" "$APPDATA_ROOT" || exit 1

print_header "Docker Stacks"

# Report every ${VAR} a compose file needs that neither the stack's .env nor the
# current environment defines. Interpolating an undefined variable is not an
# error to docker compose -- it substitutes empty and carries on, which turns
# Host(`x.${DOMAIN}`) into Host(`x.`) and silently unroutes the service. Forms
# carrying their own default (${VAR:-y}) are excluded by the pattern.
missing_compose_vars() {
    local compose="$1"
    local env_file="$2"
    local names name
    local -a missing=()

    names=$(grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' "$compose" 2>/dev/null \
        | sed -E 's/^\$\{//; s/\}$//' | sort -u)

    for name in $names; do
        [[ -n "${!name-}" ]] && continue
        if [[ -f "$env_file" ]] \
            && grep -qE "^[[:space:]]*(export[[:space:]]+)?${name}=" "$env_file"; then
            continue
        fi
        missing+=("$name")
    done

    [[ ${#missing[@]} -eq 0 ]] && return 0
    printf '%s ' "${missing[@]}"
    return 1
}

# Report containers belonging to this stack whose compose service no longer
# exists in the incoming definition -- i.e. a renamed or deleted service.
#
# `docker compose up -d` does NOT remove these; it creates the new container and
# leaves the old one running, so a rename silently doubles the service. For
# crowdsec that means two LAPI instances on one SQLite database; for cloudflared,
# two connectors on one tunnel. Detect it before copying anything so the stack is
# left entirely alone until the old container is removed by hand.
renamed_away_containers() {
    local compose="$1"
    local dest_dir="$2"
    local stack="$3"
    local defined running service name
    local -a stale=()

    command -v docker >/dev/null 2>&1 || return 0

    # Authoritative service list for the incoming file, with .env resolved from
    # the stack's own directory.
    defined=$(docker compose -f "$compose" --project-directory "$dest_dir" \
        config --services 2>/dev/null) || return 0

    running=$(docker ps -a \
        --filter "label=com.docker.compose.project=${stack}" \
        --format '{{.Label "com.docker.compose.service"}}|{{.Names}}' 2>/dev/null) || return 0

    while IFS='|' read -r service name; do
        [[ -z "$service" ]] && continue
        grep -qxF "$service" <<<"$defined" && continue
        stale+=("$name")
    done <<<"$running"

    [[ ${#stale[@]} -eq 0 ]] && return 0
    printf '%s ' "${stale[@]}"
    return 1
}

apply_stack() {
    local stack="$1"
    local dest_dir="$2"

    if ! command -v docker >/dev/null 2>&1; then
        print_warn "$stack changed but docker is unavailable; not applied"
        return 1
    fi

    print_sub "Applying $stack..."
    if (cd "$dest_dir" && docker compose up -d); then
        print_ok "$stack applied"
        return 0
    fi

    print_error "$stack failed to apply"
    return 1
}

managed=0
changed=0
applied=0
skipped=0
failed=0
declare -a managed_stacks=()

for compose_src in "$STACKS_DIR"/*/compose.yml; do
    [[ -e "$compose_src" ]] || continue

    stack="$(basename "$(dirname "$compose_src")")"
    dest_dir="${APPDATA_ROOT}/${stack}"
    dest="${dest_dir}/compose.yml"
    managed=$((managed + 1))
    managed_stacks+=("$stack")

    # The appdata directory must already exist. This module owns compose.yml and
    # nothing else -- the stack's .env, config and data are host-local and were
    # never in the repo. A missing directory therefore does not mean "new stack",
    # it means the stack is declared on a host that has none of its state: almost
    # always a placement moved in hosts.conf without the data being moved with it.
    # Creating it would fabricate an empty stack and start containers against no
    # config, so refuse. Genuinely new stacks are onboarded by creating the
    # directory and its .env on the host first.
    if [[ ! -d "$dest_dir" ]]; then
        print_warn "$stack: $dest_dir does not exist on this host"
        print_warn "  Refusing to create it. A repo-managed stack with no appdata directory"
        print_warn "  usually means it was moved to another host without its config/.env/data."
        print_warn "  New stack? Create the directory and its .env on the host, then redeploy."
        skipped=$((skipped + 1))
        continue
    fi

    missing_names=""
    if ! missing_names=$(missing_compose_vars "$compose_src" "${dest_dir}/.env"); then
        print_warn "$stack needs undefined variable(s): ${missing_names% }; leaving host copy untouched"
        skipped=$((skipped + 1))
        continue
    fi

    stale_names=""
    if ! stale_names=$(renamed_away_containers "$compose_src" "$dest_dir" "$stack"); then
        print_warn "$stack renames/removes running container(s): ${stale_names% }"
        print_warn "  docker compose up -d would leave them running alongside the new ones."
        print_warn "  Remove them first:  docker rm -f ${stale_names% }"
        print_warn "  Leaving host copy untouched."
        skipped=$((skipped + 1))
        continue
    fi

    rc=0
    copy_if_changed "$compose_src" "$dest" "$stack/compose.yml" || rc=$?
    if [[ $rc -eq 2 ]]; then
        failed=$((failed + 1))
        continue
    fi

    if [[ $rc -eq 0 ]]; then
        changed=$((changed + 1))
        if [[ "$APPLY_CHANGED" == "true" ]]; then
            if apply_stack "$stack" "$dest_dir"; then
                applied=$((applied + 1))
            else
                failed=$((failed + 1))
            fi
        else
            print_sub "$stack changed; apply disabled, not reconciled"
        fi
    fi
done

# Stacks the host runs that the repo does not describe. Reported so the drift is
# visible; never removed, because repo coverage is deliberately partial.
declare -a orphans=()
for compose_dst in "$APPDATA_ROOT"/*/compose.yml; do
    [[ -e "$compose_dst" ]] || continue
    stack="$(basename "$(dirname "$compose_dst")")"
    found=false
    for known in "${managed_stacks[@]}"; do
        [[ "$known" == "$stack" ]] && found=true && break
    done
    [[ "$found" == false ]] && orphans+=("$stack")
done

if [[ ${#orphans[@]} -gt 0 ]]; then
    print_warn "unmanaged stacks on host (not in repo, left untouched): ${orphans[*]}"
fi

print_action "managed=$managed changed=$changed applied=$applied skipped=$skipped failed=$failed unmanaged=${#orphans[@]}"

if [[ $failed -gt 0 ]]; then
    print_error "$failed stack(s) failed"
    exit 1
fi

if [[ $skipped -gt 0 ]]; then
    print_error "$skipped stack(s) skipped; see warnings above (missing appdata directory, undefined variable, or a rename needing manual container removal)"
    exit 1
fi

print_header "Docker Stacks Complete"
