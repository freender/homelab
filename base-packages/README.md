# base-packages

Installs the baseline package set on every apt-managed host, so the tools other
modules and operators assume are present are actually guaranteed by the repo.

```bash
./deploy --dry-run base-packages all
./deploy base-packages all
```

Idempotent: it checks `dpkg -s` per package and only runs apt when something is
missing, so a no-op run touches nothing and does not hit the network.

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

## Per-host additions

Extra packages go in `hosts.conf`, appended to the baseline:

```yaml
tower:
  features:
    base-packages:
      extra:
        - smartmontools
```

## Why this module exists

`mbuffer`, `vim` and `mc` used to be installed by `pve-postinstall`, which
covers only the four PVE nodes. Everything else — tower, helm, neo, riven, arc,
xur, deepstone, ghost, and the offsite hosts — had these packages only because
someone once installed them by hand, and would have lost them on a rebuild.

That is also why this module is **first in `MODULE_ORDER`**: `zfs-automation`
builds replication jobs that pipe through `mbuffer`, so the package has to exist
before those units are installed, not after.
