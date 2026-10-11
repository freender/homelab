"""Errors raised by homelab_install."""

from __future__ import annotations


class InstallError(Exception):
    """A recoverable install failure.

    Callable by any module. The library itself never calls ``sys.exit`` -- only
    the ``run()`` harness does that, on the module's behalf, after catching this.
    A module may also catch ``InstallError`` itself and continue past one failed
    item (`docker-stacks/scripts/install.py`'s `sync_stack` does exactly that: one
    stack failing does not abort the others).
    """
