# nvidia-gpu

Headless NVIDIA compute stack for a GPU passthrough VM: NVIDIA's CUDA apt repo,
the driver pinned to one branch, the open kernel modules via DKMS, and the
NVIDIA Container Toolkit so containers can use `docker run --gpus all`.

The node side of passthrough (vfio binding, the `pci-gpu-nvidia` mapping) is
`pve-gpu-passthrough`; this module runs inside the guest.

## Configuration

```yaml
glint:
  features:
    docker:
    nvidia-gpu:
      branch: 595
```

`branch` is required. NVIDIA's `nvidia-driver-pinning-<branch>` package holds
every driver package at that branch, so the daily `apt-upgrade` dist-upgrade
takes point releases and never a new branch. To move branches, change the value
and redeploy; the deploy installs the new pin and then refuses until a
dist-upgrade has moved the driver onto it.

Debian only (the repo path is `debian<VERSION_ID>`), amd64 only.

## What it installs

| Item | From |
| --- | --- |
| `/etc/apt/keyrings/nvidia-cuda.asc` | `configs/` (fingerprint `02182E60104FCDC26EAE1B8597A5D4CB8793F200`) |
| `/etc/apt/sources.list.d/nvidia-cuda.list` | written on the host from `/etc/os-release` |
| `nvidia-driver-pinning-<branch>` | installed first, in its own apt call |
| `linux-headers-amd64`, `nvidia-driver-cuda`, `nvidia-kernel-open-dkms` | the driver |
| `nvidia-container-toolkit` | same repo |

`nvidia-kernel-support` blacklists nouveau and rebuilds the initramfs itself.
Docker's `daemon.json` is left to the `docker` module: `--gpus` does not need an
`nvidia` runtime registered there.

## Reboots

The module never reboots. After a first install, or a driver update, the
deploy warns that the kernel module is not loaded or is out of date; reboot the
guest to pick it up. Secure Boot must be off (the `linux-vm` profile's efidisk
has no pre-enrolled keys), since the DKMS module is unsigned.
