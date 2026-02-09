# Bind devices to vfio-pci for passthrough.
options vfio-pci ids=${PCI_IDS} disable_vga=1

# Some platforms need this for interrupt remapping quirks.
options vfio_iommu_type1 allow_unsafe_interrupts=1
