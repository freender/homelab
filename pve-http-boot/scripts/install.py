#!/usr/bin/env python3
"""Remote installer for the pve-http-boot module (freender/homelab-ops#30).

Installs the UEFI HTTP Boot server on `arc`: the nginx vhost, the iPXE entry point,
the SNP-bound loader, the PDM answer-auth token, the operational scripts, and the
`pve-http-boot-autoupdate` service and timer. The boot payload itself (`boot.ipxe`,
`vmlinuz`, `initrd.img`, the prepared ISO) is never shipped here -- the autoupdate
job builds it at runtime from `prepare-iso` output.

Behaviour changes from `install.sh`:

* **Converged files are not rewritten, and nginx is not restarted on every deploy.**
  The bash `install`ed the loader and the token unconditionally and restarted a
  running nginx each run. nginx now restarts only when its vhost, the site link, or
  the default site changed, and never starts when it is stopped, same as before.
* **A rejected vhost is rolled back.** The bash installed it, then ran `nginx -t` and
  exited 1 with the broken file left in place for the next nginx restart to trip on.
  `files.install_validated` restores the previous file, and `nginx -t` runs again
  before any restart, since a site link change alters the config without the vhost.
* **A failed restart fails the deploy.** It was `restart nginx || true`.
* **A failed Proxmox key fetch fails the deploy.** The bash warned, skipped the repo,
  and let `apt-get install proxmox-auto-install-assistant` fail on a package that did
  not exist yet. The key is written through a temporary file, so a dropped
  connection cannot leave a truncated key behind for the repo to trust.
* **Packages are checked through dpkg** (`packages.ensure`), not `command -v`, and the
  lists are refreshed after the repo is added even when `curl` was installed earlier
  in the same run (`packages.sources_changed`).
* **A changed timer is restarted** through `systemd.ensure_running`, like every other
  ported module's timer, rather than only daemon-reloaded and started if inactive.

**Completed migrations are not ported.** Each was checked on `arc`, the only host with
this feature, before being dropped, and a host built fresh never had any of them: the
retired node_exporter textfile directory, the legacy proxyPXE/TFTP files, the baked
offsite ISO builder and its answer files, the `pxe` nginx site, the seven hand-rolled
iPXE menus, and the old homelab `boot.ipxe` matched by content. `dnsmasq` is installed
on `arc` but already disabled, and nothing here reinstalls or enables it.

The flock-guarded cleanup of `/srv/httpboot.stage` and `/root/iso/http-boot-build` is
also gone: `pve-http-boot-autoupdate` `rm -rf`s both itself before it uses them, so
the installer was cleaning up after a job that already cleans up after itself.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from homelab_install import files, log, packages, run, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection point for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run

BOOTSTRAP_PACKAGES = ("curl", "ca-certificates")
# `proxmox-auto-install-assistant` comes from the Proxmox repo below, which is why
# this set is ensured only after that repo exists. `util-linux` provides `flock`,
# which the autoupdate job takes its lock with.
PACKAGES = ("nginx", "rsync", "util-linux", "xorriso", "ipxe", "proxmox-auto-install-assistant")

PROXMOX_REPO = "/etc/apt/sources.list.d/proxmox-pve.list"
PROXMOX_REPO_LINE = "deb http://download.proxmox.com/debian/pve trixie pve-no-subscription\n"
PROXMOX_KEY = "/etc/apt/trusted.gpg.d/proxmox-release-trixie.gpg"
PROXMOX_KEY_URL = "https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg"

VHOST = "nginx-http-boot.conf"
SITE_AVAILABLE = "/etc/nginx/sites-available/http-boot"
SITE_LINK = "/etc/nginx/sites-enabled/http-boot"
# Created by the nginx package's postinst on a fresh host, where it claims port 80
# as `default_server` ahead of the HTTP Boot vhost.
DEFAULT_SITE = "/etc/nginx/sites-enabled/default"

# Serve snponly.efi, not ipxe.efi. ipxe.efi carries iPXE's own NIC drivers and
# resets the card when it takes over, so the link drops and must renegotiate. That
# is free on a virtio NIC and has never once succeeded on this fleet's bare metal:
# every node here boots from an HP 560SFP+ (Intel 82599), where iPXE re-inits the
# card and then sits on "Waiting for link-up" until it gives up. snponly.efi binds
# to the UEFI Simple Network Protocol instead, reusing the option-ROM driver that
# already brought the link up and fetched this very file.
#
# The destination keeps the name ipxe.efi deliberately: it is the URL baked into the
# UniFi Network Boot DHCP option, and renaming it strands every client until that
# option is edited by hand.
LOADER_SOURCE = "/usr/lib/ipxe/snponly.efi"
LOADER_DEST = "/srv/httpboot/httpboot/ipxe.efi"

# Staged into build/<host>/ from the tmpfs secret cache by the orchestrator, and
# only on a live run with 1Password reachable -- so its absence is not an error.
TOKEN_NAME = "homelab-pve-auto-install.token"
TOKEN_DEST = "/root/homelab-pve-auto-install.token"

SERVICE = "pve-http-boot-autoupdate.service"
TIMER = "pve-http-boot-autoupdate.timer"
BOOT_MENU = "/srv/httpboot/boot.ipxe"


def ensure_proxmox_repo(ctx: InstallContext) -> None:
    """Add the Proxmox no-subscription repo, the only source of
    `proxmox-auto-install-assistant`. Only ever creates it: an existing list is left
    exactly as it is."""
    if Path(PROXMOX_REPO).is_file():
        log.sub("Proxmox repo already configured")
        return

    log.action("Adding Proxmox no-subscription apt repo")
    partial = f"{PROXMOX_KEY}.partial"
    result = _run(["curl", "-fsSL", PROXMOX_KEY_URL, "-o", partial], check=False)
    if result.returncode != 0:
        Path(partial).unlink(missing_ok=True)
        raise InstallError(
            f"could not fetch the Proxmox release key from {PROXMOX_KEY_URL} "
            f"(curl exited {result.returncode}); proxmox-auto-install-assistant needs that repo"
        )
    os.replace(partial, PROXMOX_KEY)
    Path(PROXMOX_KEY).chmod(0o644)

    Path(PROXMOX_REPO).write_text(PROXMOX_REPO_LINE, encoding="utf-8")
    Path(PROXMOX_REPO).chmod(0o644)
    packages.sources_changed(ctx)
    log.ok("Proxmox repo added")


def enable_site(ctx: InstallContext) -> None:
    """Link the HTTP Boot vhost into sites-enabled and drop the package default."""
    link = Path(SITE_LINK)
    if not (link.is_symlink() and os.readlink(link) == SITE_AVAILABLE):
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(SITE_AVAILABLE)
        ctx.changes.record(SITE_LINK)
        log.sub("Enabled nginx http-boot site")

    # `-L`, not `exists()`: a dangling default link still enables a site nginx
    # will refuse to load.
    default = Path(DEFAULT_SITE)
    if default.is_symlink():
        default.unlink()
        ctx.changes.record(DEFAULT_SITE)
        log.sub("Disabled nginx default site")


def install_token(ctx: InstallContext) -> None:
    """Install the PDM answer-auth token when this run staged one. Never logged."""
    staged = ctx.build_dir / TOKEN_NAME
    if staged.is_file():
        log.action("Installing PDM answer-auth token")
        files.install_from(ctx, staged, TOKEN_DEST, "600", record=TOKEN_NAME)
    elif Path(TOKEN_DEST).is_file():
        log.sub("Token not staged; keeping the one already on the host")
    else:
        log.warn(
            "Token not staged and not present on host; "
            "run the deploy online to install it from 1Password"
        )


def _nginx_active() -> bool:
    return _run(["systemctl", "is-active", "--quiet", "nginx"], check=False).returncode == 0


def apply_nginx(ctx: InstallContext) -> None:
    """Restart a running nginx when its configuration changed, after `nginx -t`.

    Restart rather than reload, so a changed `listen` directive takes effect. A
    stopped nginx stays stopped: `pve-http-boot-enable` is how it is started, and a
    deploy that brought it up would override that choice.
    """
    if not ctx.changes.touched(VHOST, SITE_LINK, DEFAULT_SITE):
        log.sub("nginx configuration unchanged; not restarting")
        return

    test = _run(["nginx", "-t"], check=False)
    if test.returncode != 0:
        raise InstallError(f"nginx -t failed (exit {test.returncode}); nginx not restarted")
    log.ok("nginx config valid")

    if not _nginx_active():
        log.sub("nginx is not running; leaving it stopped")
        return

    restart = _run(["systemctl", "restart", "nginx"], check=False)
    if restart.returncode != 0:
        raise InstallError(f"systemctl restart nginx failed (exit {restart.returncode})")
    log.ok("nginx restarted")


def ensure_payload(ctx: InstallContext) -> None:
    """Start a payload build when nothing is being served yet.

    `boot.ipxe`, `vmlinuz`, `initrd.img` and the prepared ISO are all built by the
    autoupdate job, so a fresh host has nothing to serve until it runs, and the
    timer is weekly. Started detached: the build takes minutes and its result
    belongs in the journal, not in the deploy output.
    """
    menu = Path(BOOT_MENU)
    if menu.is_file() and menu.stat().st_size > 0:
        return

    log.action("No boot payload present; starting pve-http-boot-autoupdate")
    result = _run(["systemctl", "start", "--no-block", SERVICE], check=False)
    if result.returncode != 0:
        log.warn(f"could not queue {SERVICE} (exit {result.returncode}); no payload to serve yet")
        return
    log.sub(f"Watch: journalctl -fu {SERVICE}")


def install(ctx: InstallContext) -> None:
    log.header("PVE HTTP Boot")

    packages.ensure(ctx, *BOOTSTRAP_PACKAGES)
    ensure_proxmox_repo(ctx)
    packages.ensure(ctx, *PACKAGES)

    log.action("Installing managed HTTP Boot files")
    enable_site(ctx)
    files.install_all(ctx, exclude=(VHOST,))
    files.install_validated(ctx, VHOST, ["nginx", "-t"])

    log.action("Installing HTTP Boot loader")
    files.install_from(ctx, Path(LOADER_SOURCE), LOADER_DEST, "644", record=LOADER_DEST)

    install_token(ctx)

    log.action("Installing pve-http-boot-autoupdate systemd units")
    # Reloaded here as well as inside `ensure_running`, which only reloads for a
    # changed *timer*: a service-only change would otherwise run from systemd's
    # cached definition on the next fire.
    if ctx.changes.touched(SERVICE):
        systemd.daemon_reload(ctx)
    systemd.ensure_running(ctx, TIMER, changed=ctx.changes.touched(TIMER))

    apply_nginx(ctx)
    ensure_payload(ctx)

    mgmt_ip = ctx.deploy_env.get("HTTP_BOOT_MGMT_IP", "").strip()
    if mgmt_ip:
        log.sub(f"UniFi Network Boot filename: http://{mgmt_ip}/httpboot/ipxe.efi")
    log.sub("Run: pve-http-boot-enable   (to ensure nginx is serving HTTP Boot)")
    log.sub("Run: pve-http-boot-disable  (note: disable UniFi Network Boot to stop clients)")
    log.sub("Run: pve-http-boot-autoupdate  (to detect and promote a new PVE ISO)")


if __name__ == "__main__":
    run(install, "PVE HTTP Boot")
