"""The Alertmanager `scheduled-maintenance` mute windows, read from the repo.

monitoring-config owns the windows; this reads them rather than restating them.
A reboot inside one is refused because the mute would hide what the reboot
breaks. At plan time one about to open is a warning, since a roll takes a
while; between waves (`verify`) one opening within the next wave is a stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .checks import STOP, WARN, Finding

INTERVAL = "scheduled-maintenance"
ALERTMANAGER = Path("monitoring-config") / "configs" / "alertmanager.yml.tpl"
LOOKAHEAD = timedelta(minutes=60)
# One wave: the reboot plus `/roll`'s `./roll verify --wait 900`.
WAVE = timedelta(minutes=15)


@dataclass(frozen=True)
class Window:
    start: time
    end: time
    location: str


def parse_windows(text: str) -> list[Window]:
    data = yaml.safe_load(text) or {}
    for interval in data.get("time_intervals") or []:
        if interval.get("name") == INTERVAL:
            return [
                Window(
                    time.fromisoformat(span["start_time"]),
                    time.fromisoformat(span["end_time"]),
                    entry.get("location", "UTC"),
                )
                for entry in interval.get("time_intervals") or []
                for span in entry.get("times") or []
            ]
    return []


def load_windows(root: Path) -> list[Window]:
    path = root / ALERTMANAGER
    return parse_windows(path.read_text(encoding="utf-8")) if path.is_file() else []


def window_findings(
    windows: list[Window], now: datetime, lookahead: timedelta = LOOKAHEAD, ahead: str = WARN
) -> list[Finding]:
    """`now` must be timezone-aware. A window opening within `lookahead` is `ahead`."""
    if not windows:
        return [Finding(WARN, "schedule", f"no {INTERVAL} windows found; not checked")]
    findings: list[Finding] = []
    for window in windows:
        local = now.astimezone(ZoneInfo(window.location))
        label = f"{window.start:%H:%M}-{window.end:%H:%M} {window.location}"
        opens = datetime.combine(local.date(), window.start, local.tzinfo) - local
        if window.start <= local.time() < window.end:
            findings.append(Finding(STOP, "schedule", f"inside the {label} maintenance window"))
        elif timedelta(0) < opens <= lookahead:
            minutes = int(opens.total_seconds() // 60)
            findings.append(
                Finding(ahead, "schedule", f"the {label} maintenance window opens in {minutes} min")
            )
    return findings


def wave_findings(windows: list[Window], now: datetime) -> list[Finding]:
    """Before the next wave: stop inside a window or when one opens during the wave."""
    return window_findings(windows, now, WAVE, STOP)
