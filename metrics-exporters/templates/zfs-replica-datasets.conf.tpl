# Managed by homelab metrics-exporters. Do not edit by hand.
#
# Datasets this host receives as a ZFS push target. zfs-pool-textfile-exporter
# reports the creation time of each one's newest snapshot as
# homelab_zfs_replica_last_snapshot_timestamp_seconds (0 when the dataset is
# missing or has no snapshot yet), so a push that stops arriving is alertable
# whatever the reason: failed, hung, never scheduled, or source gone quiet.
#
# Source: zfs-automation.push_target_access.datasets in hosts.conf.
{% for dataset in ZFS_REPLICA_DATASETS %}
{{ dataset }}
{% endfor %}
