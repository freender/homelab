"""Output helpers, frozen byte-for-byte on `lib/print.sh` (decision 5 of
freender/homelab-ops#31). No new glyphs, no footer/summary logic here -- the
`<Module> Complete` footer belongs to the run() harness, not this module.
"""

from __future__ import annotations

import sys


def header(message: str) -> None:
    print(f"=== {message} ===")


def action(message: str) -> None:
    print(f"==> {message}")


def sub(message: str) -> None:
    print(f"    {message}")


def ok(message: str) -> None:
    print(f"    \u2713 {message}")


def warn(message: str) -> None:
    print(f"    \u2717 Warning: {message}")


def error(message: str) -> None:
    print(f"    \u2717 Error: {message}", file=sys.stderr)
