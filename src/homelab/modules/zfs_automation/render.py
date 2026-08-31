"""Render generated bash: sanoid config, snapshot script, replication script,
and authorized_keys files for the zfs-push service account.

These functions build root-executed shell scripts as Python string
concatenation rather than through the repo's Jinja templating (used
everywhere else) — a known wart, tracked separately from this package split.
Keeping it isolated in one submodule at least contains the blast radius and
makes it the obvious place to convert to Jinja later.
"""

from __future__ import annotations

from shlex import quote

from .types import (
    KnownHostRefresh,
    ReplicationPlan,
    SnapshotPlan,
    ZfsPushTargetAccess,
)


def sanoid_plan_lines(plan: SnapshotPlan) -> list[str]:
    lines = [
        f"[{plan.dataset}]",
        f"recursive = {'yes' if plan.recursive else 'no'}",
        f"process_children_only = {'yes' if plan.process_children_only else 'no'}",
        f"hourly = {plan.hourly}",
        f"daily = {plan.daily}",
        f"weekly = {plan.weekly}",
        f"monthly = {plan.monthly}",
        f"yearly = {plan.yearly}",
        "autosnap = yes",
        "autoprune = yes",
        "",
    ]

    return lines


def build_sanoid_config(
    snapshot_plans: list[SnapshotPlan],
) -> str:
    lines: list[str] = []
    for plan in snapshot_plans:
        lines.extend(sanoid_plan_lines(plan))

    return "\n".join(lines).rstrip() + "\n"


def append_snapshot_plan_script(
    lines: list[str],
    plan: SnapshotPlan,
    config_lines: list[str],
) -> None:
    lines.extend(
        [
            f"echo {quote(f'Including snapshot plan: {plan.dataset}')}",
            "cat >> \"$CONFIG_FILE\" <<'SANOID_CONFIG'",
            *config_lines,
            "SANOID_CONFIG",
            "PLAN_COUNT=$((PLAN_COUNT + 1))",
            "",
        ]
    )


def build_snapshot_script(
    snapshot_plans: list[SnapshotPlan],
) -> str:
    lines = [
        "#!/bin/bash",
        "",
        "set -euo pipefail",
        "",
        'CONFIG_DIR="$(mktemp -d)"',
        "# shellcheck disable=SC2034",
        'CONFIG_FILE="$CONFIG_DIR/sanoid.conf"',
        'trap \'rm -rf "$CONFIG_DIR"\' EXIT',
        "PLAN_COUNT=0",
        "",
        "require_dataset() {",
        "  local dataset=\"$1\"",
        "  if ! zfs list -H -o name \"$dataset\" >/dev/null 2>&1; then",
        "    echo \"ERROR: active snapshot dataset missing: $dataset\" >&2",
        "    exit 1",
        "  fi",
        "}",
        "",
    ]

    if not snapshot_plans:
        lines.extend(["echo 'No snapshot plans configured; nothing to do'", "exit 0", ""])
    else:
        for plan in snapshot_plans:
            config_lines = sanoid_plan_lines(plan)
            if plan.require_active_lxc is None:
                append_snapshot_plan_script(lines, plan, config_lines)
                continue

            unit_name = f"pve-container@{plan.require_active_lxc}.service"
            active_message = f"Active LXC {plan.require_active_lxc}; snapshotting {plan.dataset}"
            skip_message = (
                f"Skipping {plan.dataset}; LXC {plan.require_active_lxc} is not active locally"
            )
            lines.extend(
                [
                    f"if systemctl is-active --quiet {quote(unit_name)}; then",
                    f"  echo {quote(active_message)}",
                    f"  require_dataset {quote(plan.dataset)}",
                    "  cat >> \"$CONFIG_FILE\" <<'SANOID_CONFIG'",
                    *config_lines,
                    "SANOID_CONFIG",
                    "  PLAN_COUNT=$((PLAN_COUNT + 1))",
                    "else",
                    f"  echo {quote(skip_message)}",
                    "fi",
                    "",
                ]
            )

    lines.extend(
        [
            "if [[ $PLAN_COUNT -eq 0 ]]; then",
            "  echo 'No active snapshot plans selected; nothing to do'",
            "  exit 0",
            "fi",
            "",
            "/usr/sbin/sanoid --configdir=\"$CONFIG_DIR\" --cron --verbose",
            "",
        ]
    )
    return "\n".join(lines)


def shell_array_block(name: str, values: list[str]) -> str:
    lines = [f"{name}=("]
    for value in values:
        lines.append(f"  {quote(value)}")
    lines.append(")")
    return "\n".join(lines)


def build_replication_script(
    replication_plans: list[ReplicationPlan],
    syncoid_options: list[str],
    delete_target_snapshots: bool,
) -> str:
    syncoid_options_block = shell_array_block("SYNCOID_OPTIONS", syncoid_options)
    lines = [
        "#!/bin/bash",
        "",
        "set -euo pipefail",
        "",
        syncoid_options_block,
        "",
        'SCRIPT_LOCK_FILE="/run/lock/$(basename "$0").lock"',
        'GLOBAL_LOCK_FILE="/run/lock/homelab-zfs-replication.lock"',
        'exec 9>"$SCRIPT_LOCK_FILE"',
        "if ! flock -n 9; then",
        '  echo "$(basename "$0") is already running; exiting"',
        "  exit 0",
        "fi",
        'exec 8>"$GLOBAL_LOCK_FILE"',
        "flock 8",
        "",
        "wait_for_existing_replication() {",
        "  while pgrep -af '(/usr/sbin/syncoid|zfs receive|zfs send)' >/dev/null; do",
        '    echo "Waiting for existing ZFS replication process to finish"',
        "    pgrep -af '(/usr/sbin/syncoid|zfs receive|zfs send)' || true",
        "    sleep 300",
        "  done",
        "}",
        "",
        "sshkey_from_options() {",
        "  local option",
        "  for option in \"$@\"; do",
        "    case \"$option\" in",
        "      --sshkey=*) printf '%s\\n' \"${option#--sshkey=}\"; return 0 ;;",
        "    esac",
        "  done",
        "  return 1",
        "}",
        "",
        "list_snapshot_names() {",
        "  local dataset_ref=\"$1\"",
        "  shift",
        "  local dataset remote sshkey",
        "",
        "  if [[ \"$dataset_ref\" == *:* ]]; then",
        "    remote=\"${dataset_ref%%:*}\"",
        "    dataset=\"${dataset_ref#*:}\"",
        "    if sshkey=\"$(sshkey_from_options \"$@\")\" || \\",
        "      sshkey=\"$(sshkey_from_options \"${SYNCOID_OPTIONS[@]}\")\"; then",
        "      ssh -i \"$sshkey\" \"$remote\" zfs list -H -t snapshot -o name \\",
        "        -s creation \"$dataset\" | sed \"s#^${dataset}@##\"",
        "    else",
        "      ssh \"$remote\" zfs list -H -t snapshot -o name -s creation \\",
        "        \"$dataset\" | sed \"s#^${dataset}@##\"",
        "    fi",
        "  else",
        "    dataset=\"$dataset_ref\"",
        "    zfs list -H -t snapshot -o name -s creation \"$dataset\" | sed \"s#^${dataset}@##\"",
        "  fi",
        "}",
        "",
        "require_common_snapshot_lineage() {",
        "  local source=\"$1\"",
        "  local target=\"$2\"",
        "  shift 2",
        "  local source_snaps target_snaps common_snaps",
        "",
        "  if ! zfs list -H -o name \"$target\" >/dev/null 2>&1; then",
        "    return 0",
        "  fi",
        "",
        "  source_snaps=\"$(mktemp)\"",
        "  target_snaps=\"$(mktemp)\"",
        "  common_snaps=\"$(mktemp)\"",
        "",
        "  list_snapshot_names \"$source\" \"$@\" | LC_ALL=C sort -u > \"$source_snaps\"",
        "  list_snapshot_names \"$target\" | LC_ALL=C sort -u > \"$target_snaps\"",
        "",
        "  if [[ ! -s \"$target_snaps\" ]]; then",
        "    echo \"ERROR: target $target exists but has no snapshots; refusing\" \\",
        "      \"replication without known lineage\" >&2",
        "    exit 1",
        "  fi",
        "  if [[ ! -s \"$source_snaps\" ]]; then",
        "    echo \"ERROR: source $source has no snapshots; refusing replication\" >&2",
        "    exit 1",
        "  fi",
        "",
        "  LC_ALL=C comm -12 \"$source_snaps\" \"$target_snaps\" > \"$common_snaps\"",
        "  if [[ ! -s \"$common_snaps\" ]]; then",
        "    echo \"ERROR: source $source and target $target have no common\" \\",
        "      \"snapshots; refusing destructive replication\" >&2",
        "    exit 1",
        "  fi",
        "",
        "  rm -f \"$source_snaps\" \"$target_snaps\" \"$common_snaps\"",
        "}",
        "",
        "wait_for_existing_replication",
        "",
    ]
    if not replication_plans:
        lines.extend(["echo 'No replication plans configured; nothing to do'", ""])
    else:
        for plan in replication_plans:
            inactive_message = ""
            if plan.require_active_lxc is not None:
                unit_name = f"pve-container@{plan.require_active_lxc}.service"
                inactive_message = (
                    f"Skipping {plan.source}; LXC {plan.require_active_lxc} is not active locally"
                )
                active_message = f"Active LXC {plan.require_active_lxc}; replicating {plan.source}"
                lines.extend(
                    [
                        f"if systemctl is-active --quiet {quote(unit_name)}; then",
                        f"  echo {quote(active_message)}",
                    ]
                )
            lines.append(
                f"require_common_snapshot_lineage {quote(plan.source)} {quote(plan.target)}"
            )
            command = [
                "/usr/sbin/syncoid",
                "-r",
                '"${SYNCOID_OPTIONS[@]}"',
                plan.source,
                plan.target,
            ]

            if delete_target_snapshots:
                command[2:2] = ["--delete-target-snapshots", "--force-delete"]
            lines.append(
                " ".join(
                    item
                    if item in {
                        '"${SYNCOID_OPTIONS[@]}"',
                    }
                    else quote(item)
                    for item in command
                )
            )
            if inactive_message:
                lines.extend(["else", f"  echo {quote(inactive_message)}", "fi"])
            lines.append("")

    lines.append("")
    return "\n".join(lines)


def build_known_host_refresh_script(entries: tuple[KnownHostRefresh, ...]) -> str:
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "",
        "refresh_known_host() {",
        "  local host=\"$1\"",
        "  local known_hosts=\"$2\"",
        "  local port=\"$3\"",
        "  local known_hosts_dir",
        "",
        "  known_hosts_dir=\"$(dirname \"$known_hosts\")\"",
        "  mkdir -p \"$known_hosts_dir\"",
        "  chmod 700 \"$known_hosts_dir\"",
        "  touch \"$known_hosts\"",
        "  chmod 600 \"$known_hosts\"",
        "",
        "  echo \"Refreshing SSH host key for $host in $known_hosts\"",
        "  ssh-keygen -R \"$host\" -f \"$known_hosts\" >/dev/null 2>&1 || true",
        "  if [[ \"$port\" == \"22\" ]]; then",
        "    ssh-keyscan -H \"$host\" >> \"$known_hosts\" 2>/dev/null",
        "  else",
        "    ssh-keyscan -H -p \"$port\" \"$host\" >> \"$known_hosts\" 2>/dev/null",
        "  fi",
        "  ssh-keygen -F \"$host\" -f \"$known_hosts\" >/dev/null",
        "}",
        "",
    ]
    if not entries:
        lines.append("echo 'No known hosts configured for refresh'")
    else:
        for entry in entries:
            lines.append(
                "refresh_known_host "
                f"{quote(entry.host)} {quote(entry.known_hosts)} {quote(str(entry.port))}"
            )
    return "\n".join(lines) + "\n"


def build_zfs_push_target_authorized_keys(access: ZfsPushTargetAccess) -> str:
    allowed_roots = " ".join(access.datasets)
    lines = []
    for pusher in access.pushers:
        options = [
            f'from="{pusher.from_address}"',
            "restrict",
            f'command="/usr/local/sbin/homelab-zfs-receive-only {allowed_roots}"',
        ]
        lines.append(f"{','.join(options)} {pusher.public_key}")
    return "\n".join(lines) + "\n"
