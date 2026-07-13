#!/bin/bash

set -euo pipefail

deny() {
    printf 'Denied ZFS pull command: %s\n' "${SSH_ORIGINAL_COMMAND:-}" >&2
    exit 1
}

ALLOWED_ROOTS=("$@")
COMMAND="${SSH_ORIGINAL_COMMAND:-}"

[[ ${#ALLOWED_ROOTS[@]} -gt 0 ]] || deny
[[ -n "$COMMAND" ]] || deny

case "$COMMAND" in
    "exit") exit 0 ;;
    "echo -n") printf '' ; exit 0 ;;
esac

case "$COMMAND" in
    *';'*|*'&'*|*'`'*|*'$'*|*'<'*|*'>'*|*$'\n'*|*$'\r'*) deny ;;
esac

case "$COMMAND" in
    homelab-lxc-active\ *)
        vmid="${COMMAND#homelab-lxc-active }"
        [[ "$vmid" =~ ^[1-9][0-9]{1,8}$ ]] || deny
        exec /usr/bin/systemctl is-active --quiet "pve-container@${vmid}.service"
        ;;
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

case "$COMMAND" in
    "command -v mbuffer"|"command -v lzop"|"command -v pv")
        command -v "${COMMAND##* }" || exit 1
        exit 0
        ;;
esac

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

IFS='|' read -r -a PIPE_SEGMENTS <<< "$COMMAND"
[[ ${#PIPE_SEGMENTS[@]} -ge 1 && ${#PIPE_SEGMENTS[@]} -le 3 ]] || deny

ZFS_SEGMENT="$(trim "${PIPE_SEGMENTS[0]}")"
split_args "$ZFS_SEGMENT" ARGV

case "${ARGV[0]:-}" in
    zfs|/sbin/zfs|/usr/sbin/zfs) ;;
    *) deny ;;
esac

case "${ARGV[1]:-}" in
    list|get|send|hold|release) ;;
    *) deny ;;
esac

FOUND_DATASET=false
for token in "${ARGV[@]:2}"; do
    [[ "$token" == -* ]] && continue
    dataset="${token%%[@#]*}"

    allowed=false
    known_pool=false
    for root in "${ALLOWED_ROOTS[@]}"; do
        if [[ "$dataset" == "$root" || "$dataset" == "$root/"* ]]; then
            allowed=true
            break
        fi
        if [[ "$dataset" == "${root%%/*}" || "$dataset" == "${root%%/*}/"* ]]; then
            known_pool=true
        fi
    done
    if [[ "$allowed" == true ]]; then
        FOUND_DATASET=true
        continue
    fi
    [[ "$known_pool" == false ]] || deny
done

[[ "$FOUND_DATASET" == true ]] || deny

if [[ ${#PIPE_SEGMENTS[@]} -eq 1 ]]; then
    exec /usr/sbin/zfs "${ARGV[@]:1}"
fi

RUN_LZOP=false
RUN_MBUFFER=false
MBUFFER_ARGS=()

for segment in "${PIPE_SEGMENTS[@]:1}"; do
    segment="$(trim "$segment")"
    split_args "$segment" PIPE_ARGV
    case "${PIPE_ARGV[0]:-}" in
        lzop|/usr/bin/lzop)
            [[ ${#PIPE_ARGV[@]} -eq 1 ]] || deny
            [[ "$RUN_LZOP" == false ]] || deny
            RUN_LZOP=true
            ;;
        mbuffer|/usr/bin/mbuffer)
            [[ "$RUN_MBUFFER" == false ]] || deny
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
            -q)
                index=$((index + 1))
                ;;
            -s|-m)
                [[ $((index + 1)) -lt ${#MBUFFER_ARGS[@]} ]] || deny
                [[ "${MBUFFER_ARGS[$((index + 1))]}" =~ ^[0-9]+[kKmMgG]?$ ]] || deny
                index=$((index + 2))
                ;;
            *) deny ;;
        esac
    done
fi

if [[ "$RUN_LZOP" == true && "$RUN_MBUFFER" == true ]]; then
    /usr/sbin/zfs "${ARGV[@]:1}" | /usr/bin/lzop | /usr/bin/mbuffer "${MBUFFER_ARGS[@]}"
elif [[ "$RUN_LZOP" == true ]]; then
    /usr/sbin/zfs "${ARGV[@]:1}" | /usr/bin/lzop
elif [[ "$RUN_MBUFFER" == true ]]; then
    /usr/sbin/zfs "${ARGV[@]:1}" | /usr/bin/mbuffer "${MBUFFER_ARGS[@]}"
else
    deny
fi
