#!/bin/bash

set -euo pipefail

deny() {
    printf 'Denied ZFS push command: %s\n' "${SSH_ORIGINAL_COMMAND:-}" >&2
    exit 1
}

ALLOWED_ROOTS=("$@")
COMMAND="${SSH_ORIGINAL_COMMAND:-}"
COMMAND="${COMMAND% 2>&1}"

[[ ${#ALLOWED_ROOTS[@]} -gt 0 ]] || deny
[[ -n "$COMMAND" ]] || deny

case "$COMMAND" in
    "exit") exit 0 ;;
    "echo -n") printf '' ; exit 0 ;;
    "ps -Ao args=") exec /bin/ps -Ao args= ;;
    "command -v mbuffer"|"command -v lzop"|"command -v pv")
        command -v "${COMMAND##* }" || exit 1
        exit 0
        ;;
esac

case "$COMMAND" in
    *'`'*|*'$'*|*'<'*|*$'\n'*|*$'\r'*) deny ;;
esac

trim() {
    local text="$1"
    text="${text#"${text%%[![:space:]]*}"}"
    text="${text%"${text##*[![:space:]]}"}"
    printf '%s' "$text"
}

split_args() {
    local segment="$1"
    local -n out_ref="$2"
    read -r -a out_ref <<< "$segment"
    for index in "${!out_ref[@]}"; do
        out_ref[$index]="${out_ref[$index]//\'/}"
        out_ref[$index]="${out_ref[$index]//\"/}"
    done
}

dataset_allowed() {
    local dataset="$1"
    local root
    for root in "${ALLOWED_ROOTS[@]}"; do
        if [[ "$dataset" == "$root" || "$dataset" == "$root/"* ]]; then
            return 0
        fi
    done
    return 1
}

split_args "$COMMAND" ARGV

if [[ "${ARGV[0]:-}" == "zpool" \
    || "${ARGV[0]:-}" == "/sbin/zpool" \
    || "${ARGV[0]:-}" == "/usr/sbin/zpool" ]]; then
    [[ "${ARGV[1]:-}" == "get" ]] || deny
    [[ "${ARGV[2]:-}" == "-o" && "${ARGV[3]:-}" == "value" ]] || deny
    [[ "${ARGV[4]:-}" == "-H" && "${ARGV[5]:-}" == "feature@extensible_dataset" ]] || deny
    [[ ${#ARGV[@]} -eq 7 ]] || deny
    requested_pool="${ARGV[6]:-}"
    pool_allowed=false
    for root in "${ALLOWED_ROOTS[@]}"; do
        if [[ "${root%%/*}" == "$requested_pool" ]]; then
            pool_allowed=true
            break
        fi
    done
    [[ "$pool_allowed" == true ]] || deny
    exec /usr/sbin/zpool get -o value -H feature@extensible_dataset "$requested_pool"
fi

if [[ "${ARGV[0]:-}" == "zfs" \
    || "${ARGV[0]:-}" == "/sbin/zfs" \
    || "${ARGV[0]:-}" == "/usr/sbin/zfs" ]]; then
    case "${ARGV[1]:-}" in
        list|get|hold|release)
            FOUND_DATASET=false
            for token in "${ARGV[@]:2}"; do
                [[ "$token" == -* ]] && continue
                dataset="${token%%[@#]*}"
                if dataset_allowed "$dataset"; then
                    FOUND_DATASET=true
                    break
                fi
            done
            [[ "$FOUND_DATASET" == true ]] || deny
            exec /usr/sbin/zfs "${ARGV[@]:1}"
            ;;
    esac
fi

IFS='|' read -r -a PIPE_SEGMENTS <<< "$COMMAND"
[[ ${#PIPE_SEGMENTS[@]} -ge 1 && ${#PIPE_SEGMENTS[@]} -le 3 ]] || deny

ZFS_SEGMENT="$(trim "${PIPE_SEGMENTS[-1]}")"
split_args "$ZFS_SEGMENT" ZFS_ARGV

case "${ZFS_ARGV[0]:-}" in
    zfs|/sbin/zfs|/usr/sbin/zfs) ;;
    *) deny ;;
esac

case "${ZFS_ARGV[1]:-}" in
    receive|recv) ;;
    *) deny ;;
esac

TARGET_DATASET="${ZFS_ARGV[-1]:-}"
[[ -n "$TARGET_DATASET" ]] || deny
dataset_allowed "$TARGET_DATASET" || deny

RUN_LZOP=false
RUN_MBUFFER=false
MBUFFER_ARGS=()
LEADING_COUNT=$((${#PIPE_SEGMENTS[@]} - 1))
for ((segment_index = 0; segment_index < LEADING_COUNT; segment_index++)); do
    segment="$(trim "${PIPE_SEGMENTS[$segment_index]}")"
    split_args "$segment" PIPE_ARGV
    case "${PIPE_ARGV[0]:-}" in
        lzop|/usr/bin/lzop)
            [[ "${PIPE_ARGV[1]:-}" == "-dfc" ]] || deny
            RUN_LZOP=true
            ;;
        mbuffer|/usr/bin/mbuffer)
            RUN_MBUFFER=true
            MBUFFER_ARGS=("${PIPE_ARGV[@]:1}")
            ;;
        *) deny ;;
    esac
done

if [[ "$RUN_MBUFFER" == true ]]; then
    index=0
    while [[ $index -lt ${#MBUFFER_ARGS[@]} ]]; do
        case "${MBUFFER_ARGS[$index]}" in
            -q) index=$((index + 1)) ;;
            -s|-m)
                [[ $((index + 1)) -lt ${#MBUFFER_ARGS[@]} ]] || deny
                [[ "${MBUFFER_ARGS[$((index + 1))]}" =~ ^[0-9]+[kKmMgG]?$ ]] || deny
                index=$((index + 2))
                ;;
            *) deny ;;
        esac
    done
fi

if [[ "$RUN_MBUFFER" == true && "$RUN_LZOP" == true ]]; then
    /usr/bin/mbuffer "${MBUFFER_ARGS[@]}" | /usr/bin/lzop -dfc | /usr/sbin/zfs "${ZFS_ARGV[@]:1}"
elif [[ "$RUN_MBUFFER" == true ]]; then
    /usr/bin/mbuffer "${MBUFFER_ARGS[@]}" | /usr/sbin/zfs "${ZFS_ARGV[@]:1}"
elif [[ "$RUN_LZOP" == true ]]; then
    /usr/bin/lzop -dfc | /usr/sbin/zfs "${ZFS_ARGV[@]:1}"
else
    /usr/sbin/zfs "${ZFS_ARGV[@]:1}"
fi
