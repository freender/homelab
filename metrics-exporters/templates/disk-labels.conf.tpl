# Managed by homelab/metrics-exporters -- do not edit by hand.
#
# Optional per-model display-name overrides for disk-label-textfile-exporter.
# Every name it produces is normally derived from the running system (ZFS pool,
# vdev position, capacity), so this file only exists for the rare disk whose
# useful name is not derivable -- typically a USB enclosure whose model string
# names the bare drive inside it rather than the product.
#
# Keyed by model, deliberately never by serial: a model is not a hardware
# identifier, so this stays safe to render from a public repo.
#
# Format: <model> = <component name>
# The model is the string in /sys/block/<dev>/device/model, matched exactly or
# as a prefix (longest wins), so a firmware revision suffix does not have to be
# pinned. The host name, position and capacity are still added automatically, so
# `My Passport = Passport` renders as "Cottonwood Passport (1TB)".
{% for model, name in DISK_LABEL_OVERRIDES %}
{{ model }} = {{ name }}
{% endfor %}
