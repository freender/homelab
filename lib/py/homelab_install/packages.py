"""Apt ensure-installed -- one of the four gaps `utils.sh` never had
(freender/homelab-ops#31 decision 3).

Grown from keepalived's needs to `base-packages`' (freender/homelab-ops#30), which
is the module that turns this from "install a thing" into a real apt helper: it is
handed a list and has to answer, per package, whether it is present.

Three things arrived with it, all demand-driven:

* **`dpkg` status instead of a PATH probe.** `probe=` asked whether *one* binary was
  on PATH and, if so, skipped installing *all* the named packages -- so
  `packages.ensure(ctx, "keepalived", "curl", probe="keepalived")` never noticed a
  missing `curl`. PATH is also the wrong question: `ripgrep` installs `rg`, and a
  library package installs no binary at all. Each package is now checked on its own.
* **A lazy `apt-get update`.** The design doc's "one per run", finally with a caller.
  It fires at most once per installer process (again only after `sources_changed`),
  and only on a run that is actually about to install something -- an all-present
  run still touches the network zero times, which is what makes this safe to leave
  first in `MODULE_ORDER`.
* **Re-verify after installing.** `base-packages/scripts/install.sh` did this
  deliberately and the comment is worth keeping: a package that resolves but fails
  to configure leaves apt exiting 0, so trusting the exit status alone reports a
  broken package as installed.
"""

from __future__ import annotations

import os
import subprocess

from . import log
from .context import InstallContext
from .errors import InstallError

# Indirection point for tests: patch `homelab_install.packages._run` instead of
# shelling out to a real apt-get. Keeps this in-process testable per
# freender/homelab-ops#31 decision 4, no subprocess-against-a-sandbox needed.
_run = subprocess.run

# Whether `apt-get update` has already run in this process. Module-level because
# the point is to coalesce across every `ensure()` call in one installer run; an
# installer process handles exactly one host, so there is nothing to key it on.
_apt_updated = False


def _installed(package: str) -> bool:
    """Whether dpkg reports `package` as installed.

    The status field is `<want> <error> <state>` and only the third word answers
    the question. Matching the whole string against `install ok installed` would
    call a held package ("hold ok installed") missing and reinstall it on every
    deploy; matching on the exit status alone would call a removed-but-not-purged
    package ("deinstall ok config-files", exit 0) installed. `base-packages`'
    `dpkg -s` had the second bug, which is the one that matters -- a purged-config
    package would be reported present and never reinstalled.
    """
    result = _run(
        ["dpkg-query", "-W", "-f=${Status}", package],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    fields = (result.stdout or "").split()
    return len(fields) == 3 and fields[2] == "installed"


def _apt_update_once() -> None:
    global _apt_updated
    if _apt_updated:
        return
    if _run(["apt-get", "update", "-qq"], check=False).returncode != 0:
        raise InstallError("apt-get update failed")
    _apt_updated = True


def sources_changed(ctx: InstallContext) -> None:
    """Mark the package lists stale after writing an apt source.

    `ensure` refreshes the lists at most once per process, which is wrong the moment
    a module adds a repo mid-run: `pve-http-boot` may already have run the update to
    install `curl`, then uses `curl` to fetch the Proxmox key, then needs a package
    that only the new repo carries. Without this the second `ensure` trusts lists
    fetched before the repo existed and apt reports the package as unknown.

    Only marks, never fetches -- a run that adds a source but has nothing left to
    install still touches the network zero times.
    """
    global _apt_updated
    _apt_updated = False


def installed(ctx: InstallContext, package: str) -> bool:
    """Whether dpkg reports `package` installed, without installing anything.

    `apt-upgrade` needs to *ask* rather than ensure: its `auto_reboot` flag
    delegates the actual reboot to `unattended-upgrades`, so a host that opted in
    without that package present must fail loudly instead of having it silently
    installed underneath. Installing it would change the host's upgrade behaviour
    as a side effect of setting a reboot flag.
    """
    return _installed(package)


def dist_upgrade(ctx: InstallContext) -> None:
    """Refresh package lists, then take every available upgrade.

    Arrives with `pve-upgrade` (freender/homelab-ops#30), whose deploy action *is*
    this call. Distinct from `ensure`: that converges a named set and is safe to
    run anywhere, this changes whatever the host happens to have pending, which is
    why its module is gated behind `--confirm-upgrade` and excluded from
    `deploy all`.

    Deliberately **not** `-q`, unlike `ensure`. This runs on demand with an
    operator watching, and the package list it prints is the only record of what
    a given upgrade actually changed -- `apt-upgrade`'s scheduled equivalent has
    a systemd journal to fall back on, and this has nothing.

    `_apt_update_once` is reused rather than an unconditional update: it is
    already exactly once per installer process, and this module's process does
    nothing else.
    """
    log.action("Running apt-get update")
    _apt_update_once()

    log.action("Running apt-get dist-upgrade")
    result = _run(
        ["apt-get", "-y", "dist-upgrade"],
        check=False,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    if result.returncode != 0:
        raise InstallError(f"apt-get dist-upgrade failed (exit {result.returncode})")


def ensure(ctx: InstallContext, *packages: str) -> None:
    """Install whichever of `packages` dpkg does not already report installed."""
    missing = [package for package in packages if not _installed(package)]
    if not missing:
        log.sub(f"All packages already installed: {' '.join(packages)}")
        return

    log.action(f"Installing missing packages: {' '.join(missing)}")
    _apt_update_once()

    # DEBIAN_FRONTEND is set on the child only. Inherited from the parent env
    # rather than replacing it, because dropping PATH here would leave apt-get
    # unable to find the maintainer-script helpers it shells out to.
    result = _run(
        ["apt-get", "install", "-y", "-q", *missing],
        check=False,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    if result.returncode != 0:
        raise InstallError(f"failed to install packages: {', '.join(missing)}")

    still_missing = [package for package in missing if not _installed(package)]
    if still_missing:
        raise InstallError(f"packages still missing after install: {', '.join(still_missing)}")

    log.ok(f"Installed: {' '.join(missing)}")
