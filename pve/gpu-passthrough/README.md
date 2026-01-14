# PVE - GPU Passthrough

GPU passthrough configuration for Proxmox VE (Intel iGPU and NVIDIA).

## Overview

This directory contains automated GPU passthrough configuration for Proxmox hosts:
- **ace:** Intel Coffee Lake iGPU (UHD Graphics 630)
- **clovis:** NVIDIA GPU (RTX 3080)

## Prerequisites

**BIOS Requirements:**
- Intel: Enable VT-d & VT-x
- AMD: Enable IOMMU (all CPUs from Bulldozer onwards)

**Network Access:**
- SSH access to target Proxmox hosts from helm
- Root privileges on target hosts

## Directory Structure

```
gpu-passthrough/
├── deploy.sh              # Deployment script (run from helm)
├── README.md              # This file
├── ace/                   # Intel iGPU config for ace
│   ├── grub               # GRUB cmdline (video=efifb:off)
│   ├── blacklist.conf     # Blacklist i915 driver
│   ├── vfio.conf          # Bind GPU to vfio-pci (8086:3e92)
│   └── modules            # VFIO kernel modules
└── clovis/                # NVIDIA config for clovis
    ├── grub               # GRUB cmdline (Intel CPU)
    ├── blacklist.conf     # Blacklist nvidia/nouveau drivers
    ├── vfio.conf          # Bind GPU to vfio-pci (10de:2208,10de:1aef)
    └── modules            # VFIO kernel modules
```

## Usage

Deploy to specific host:
```bash
cd ~/homelab/pve/gpu-passthrough
./deploy.sh ace      # Deploy to ace
./deploy.sh clovis   # Deploy to clovis
```

Deploy to all hosts:
```bash
./deploy.sh all
```

Reboot to apply:
```bash
ssh ace reboot
ssh clovis reboot
```

**Warning:** After reboot, host will have no video output. Use Proxmox web GUI at `https://<host>:8006`.

## Host Configurations

### ace (Intel Coffee Lake iGPU)

**Hardware:**
- CPU: Intel i7-8700K (Coffee Lake, 8th gen)
- GPU: Intel UHD Graphics 630 [8086:3e92]
- IOMMU Group: 0

**GRUB Parameters:**
```
intel_iommu=on pcie_acs_override=downstream video=efifb:off
```

**Notes:**
- `video=efifb:off` disables EFI framebuffer (prevents host from using GPU)
- `pcie_acs_override=downstream` required for NVMe passthrough
- Intel GVT-g not supported on Coffee Lake

**VM Configuration:**
- Machine: i440fx or q35
- BIOS: OVMF (UEFI) or SeaBIOS
- PCI Device: Enable All Functions, ROM-Bar, PCIe

**Unraid VM Boot Parameters:**
```
pci=noaer pcie_acs_override=downstream,multifunction i915.disable_display=1 video=efifb:off initrd=/bzroot
```

### clovis (NVIDIA GPU)

**Hardware:**
- CPU: Intel (VT-d enabled)
- GPU: NVIDIA RTX 3080 [10de:2208]
- Audio: NVIDIA Audio [10de:1aef]

**GRUB Parameters:**
```
intel_iommu=on iommu=pt pcie_acs_override=downstream
```

**Notes:**
- `iommu=pt` enables passthrough mode (better performance)
- Both GPU and audio controller must be passed together

**VM Configuration:**
- Machine: q35 (recommended for NVIDIA)
- BIOS: OVMF (UEFI) required for modern NVIDIA cards
- PCI Device: Enable All Functions, ROM-Bar, PCIe
- Set as Primary GPU if needed

## VM Configuration

### Machine Settings

| Setting | Intel iGPU | NVIDIA |
|---------|-----------|--------|
| Machine | i440fx or q35 | q35 |
| BIOS | OVMF (UEFI) or SeaBIOS | OVMF (UEFI) |
| QEMU Guest Agent | Enabled | Enabled |

### PCI Device Settings

All configurations require:
- ✅ Enable: **All Functions**
- ✅ Enable: **ROM-Bar**
- ✅ Enable: **PCIe** (critical for performance)
- Optional: Set as **Primary GPU**

### ROM Files (NVIDIA)

Some NVIDIA cards require VBIOS ROM file:
1. Extract ROM with GPU-Z on Windows
2. Upload to Proxmox: `/usr/share/kvm/<gpu-name>.bin`
3. Reference in VM config: `romfile=<gpu-name>.bin`

## Verification

Check IOMMU enabled:
```bash
ssh <host> "dmesg | grep -e DMAR -e IOMMU"
# Expected: Line showing "IOMMU enabled"
```

Verify GPU bound to vfio-pci:
```bash
ssh <host> "lspci -nnk | grep -A 3 -E 'VGA|3D'"
# Expected: Kernel driver in use: vfio-pci
```

Check VFIO modules loaded:
```bash
ssh <host> "lsmod | grep vfio"
# Expected: vfio_pci, vfio_iommu_type1, vfio
```

List IOMMU groups:
```bash
ssh <host> "find /sys/kernel/iommu_groups/ -type l | sort -V"
```

## Troubleshooting

### No display after reboot
**Expected behavior** - GPU is bound to vfio-pci and unavailable to host.

**Solution:** Use Proxmox web GUI at `https://<host>:8006`

### Code 43 error in Windows VM
NVIDIA driver returns Code 43 when KVM detection enabled.

**Solutions:**
1. Use OVMF (UEFI) instead of SeaBIOS
2. Enable PCIe on PCI device
3. Hide KVM detection - add to VM config:
   ```
   args: -cpu host,kvm=off
   ```
4. Use recent drivers (older drivers may not support hiding)

### GPU not working after VM restart (Reset Bug)
Some GPUs don't properly reset between VM sessions.

**Solutions:**
1. Use vendor reset kernel patch (if available)
2. Reboot Proxmox host between VM sessions
3. Try different GPU model
4. Use PCIe passthrough instead of legacy PCI

### IOMMU groups too large
Multiple devices in same IOMMU group prevent individual passthrough.

**Solution:** Use ACS override (already in GRUB configs):
```
pcie_acs_override=downstream
```

**Warning:** May reduce isolation security between devices.

### Intel iGPU: Host display frozen
If host console appears to hang during boot, this is expected.

**Cause:** GPU passed to VM, no display output available to host.

**Recovery:** See Obsidian guide: `PVE - GPU Passthrough Recovery - Ace`

## File Locations

| File | Purpose |
|------|---------|
| `/etc/default/grub` | Kernel boot parameters |
| `/etc/modules` | Kernel modules to load at boot |
| `/etc/modprobe.d/blacklist.conf` | Prevent host drivers from loading |
| `/etc/modprobe.d/vfio.conf` | Bind specific devices to vfio-pci |

**Apply changes:**
```bash
update-grub
update-initramfs -u -k all
reboot
```

## Quick Reference

**Device IDs:**
- ace: Intel UHD 630 [8086:3e92]
- clovis: NVIDIA RTX 3080 [10de:2208], Audio [10de:1aef]

**Get device IDs:**
```bash
lspci -nn | grep -E "VGA|3D|Audio controller.*NVIDIA"
```

**Deployment:**
```bash
cd ~/homelab/pve/gpu-passthrough
./deploy.sh <host>
ssh <host> reboot
```

**Verification:**
```bash
ssh <host> "lspci -nnk | grep -A 3 VGA"
```

**Related Documentation:**
- Obsidian: `Main/Homelab/Proxmox/PVE - GPU Passthrough`
- Obsidian: `Main/Homelab/Proxmox/PVE - GPU Passthrough Recovery - Ace`
