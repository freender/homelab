# base-packages

Installs the baseline package set on every apt-managed host, so the tools other
modules and operators assume are present are actually guaranteed by the repo.

```bash
./deploy --dry-run base-packages all
./deploy base-packages all
```

Idempotent: it checks dpkg's status field per package and only runs apt when
something is missing, so a no-op run touches nothing and does not hit the
network — which matters because this module is first in `MODULE_ORDER` and runs
on every host on every deploy.

The installer is `scripts/install.py`, on the shared `homelab_install` library
(freender/homelab-ops#30). It reads `BASE_PACKAGES` from the deploy environment
rather than a rendered env file, because this module stages no build directory.

## The baseline

Defined once in `src/homelab/modules/base_packages.py` (`BASE_PACKAGES`) and
passed down to the installer, so the list is never re-derived in bash.

| Package | Why |
| --- | --- |
| `mbuffer` | `zfs-automation` replication pipes send/recv through it |
| `vim` | Expected editor on every host |
| `mc` | Interactive file management |
| `ripgrep` | Standard content search |

`ripgrep` matters more than it looks. Without `rg` on a host, searching falls
back to recursive `grep`, which is precisely what the scan-boundary rules
prohibit on the multi-TB storage paths. Having it everywhere makes the fast,
bounded tool the default one.

## VM baseline

Every VM (`pve-guest.kind: qemu`) also gets `VM_BASE_PACKAGES`: tools the Debian
cloud image lacks but the LXC template had, so a new VM needs no per-host list.

| Package | Why |
| --- | --- |
| `qemu-guest-agent` | Replication's fs-freeze on live migration |
| `locales` | `guest-baseline` refuses to run without `locale-gen` |
| `iproute2`, `fdisk` | In the image today; listed so they stay after cloud-init is purged |
| `dbus-user-session`, `logrotate`, `rsync`, `lsof`, `bind9-dnsutils`, `netcat-traditional` | Operator tools the LXC template shipped |

Do not repeat these in a VM's `extra`; a test fails if you do.

## Per-host additions

Extra packages go in `hosts.conf`, appended to the baseline:

```yaml
tower:
  features:
    base-packages:
      extra:
        - smartmontools
```

## Backports kernel

**No host uses this, by decision (2026-10-17).** riven and helm ran it from
their VM builds until then and are back on stable's kernel.

The flag existed so a VM, which boots its own kernel, would match the 7.x Proxmox
kernel the PVE nodes and LXCs run (helm: the kernel its stacks ran on as CT
107). It never stayed matched. Backports follows the newest upstream stable
release (7.2 at the time), while Proxmox holds one Ubuntu-derived series (7.0)
for its whole cycle, so the VMs drifted ahead. A VM sees only virtio hardware,
whose drivers have been mature for years, so a newer kernel buys it nothing.
What it costs is real:

- Backports has no Debian security support. Fixes land only when a newer
  upstream release is rebuilt, and each upstream series goes unmaintained once
  the next one ships.
- Stable's kernel is an upstream LTS series. The security team patches it
  through `<codename>-security`, and far more users test it.
- Every new upstream release is another reboot the VM didn't otherwise need.

Use it again only for a concrete need: passthrough hardware that 6.12 can't
drive, or a kernel feature a workload requires. The mechanism:

```yaml
riven:
  features:
    base-packages:
      backports_kernel: true
```

The installer runs `apt-get install -t <codename>-backports linux-image-amd64`
once, when the installed kernel metapackage is not already a `~bpo` version.
After that, `apt-upgrade`'s dist-upgrade keeps it current, because backports is
`ButAutomaticUpgrades`. The module never reboots: `RebootRequired` reports the
pending kernel.

Setting the flag back to false does not downgrade anything, and the kernel keeps
following backports. Going back is manual, and the metapackage alone is not
enough. Backports also pulls in `linux-base`, `linux-base-amd64`,
`linux-sysctl-defaults` and, on hosts with build tools, `linux-libc-dev`, and
each of those follows backports too. Downgrade every `~bpo` package to the
highest non-backports version in `apt-cache policy`, and remove the backports
kernel's own packages in the same transaction. Dry-run it with `-s` first:

```bash
dpkg -l | awk '/^ii/ && $3 ~ /bpo/ {print $2, $3}'
apt-get install --allow-downgrades \
  linux-image-amd64=<stable> linux-base=<stable> linux-sysctl-defaults=<stable> \
  linux-libc-dev=<stable> \
  linux-image-<bpo-release>- linux-binary-<bpo-release>- \
  linux-base-<bpo-release>- linux-modules-<bpo-release>- linux-base-amd64-
```

Removing the running kernel is safe here: with `DEBIAN_FRONTEND=noninteractive`,
`linux-check-removal` does not prompt. GRUB's default then points at the stable
kernel. The running kernel no longer matches any installed one, so
the reboot-textfile-exporter raises `RebootRequired`. Reboot through the VM's procedure in
`pve-upgrade/README.md`.

## Why this module exists

`mbuffer`, `vim` and `mc` used to be installed by `pve-postinstall`, which
covers only the four PVE nodes. Everything else — tower, helm, neo, riven, arc,
xur, deepstone, ghost, and the offsite hosts — had these packages only because
someone once installed them by hand, and would have lost them on a rebuild.

That is also why this module is **first in `MODULE_ORDER`**: `zfs-automation`
builds replication jobs that pipe through `mbuffer`, so the package has to exist
before those units are installed, not after.
