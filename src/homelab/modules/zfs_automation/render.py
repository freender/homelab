"""Render generated bash: the sanoid config and the snapshot script.

These functions build root-executed shell scripts as Python string
concatenation rather than through the repo's Jinja templating (used
everywhere else) — a known wart, tracked separately from this package split.
Keeping it isolated in one submodule at least contains the blast radius and
makes it the obvious place to convert to Jinja later.
"""

from __future__ import annotations

from shlex import quote

from .types import GUEST_ACTIVE_UNITS, SnapshotPlan


def active_guest_unit(kind: str, vmid: int) -> tuple[str, str]:
    """`("LXC 101", "pve-container@101.service")` — the log label and the unit
    that is active only on the node currently running the guest."""
    label, unit = GUEST_ACTIVE_UNITS[kind]
    return f"{label} {vmid}", unit.format(vmid=vmid)


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
        f"autosnap = {'yes' if plan.autosnap else 'no'}",
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
    kind = "snapshot plan" if plan.autosnap else "prune-only plan"
    lines.extend(
        [
            f"echo {quote(f'Including {kind}: {plan.dataset}')}",
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
            if plan.require_active_guest is None:
                append_snapshot_plan_script(lines, plan, config_lines)
                continue

            label, unit_name = active_guest_unit(plan.guest_kind, plan.require_active_guest)
            active_message = f"Active {label}; snapshotting {plan.dataset}"
            skip_message = f"Skipping {plan.dataset}; {label} is not active locally"
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
