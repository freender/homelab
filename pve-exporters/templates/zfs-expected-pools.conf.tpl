# Managed by homelab pve-exporters. Do not edit by hand.
#
# Pools that must be imported on this host. zfs-pool-textfile-exporter reports
# any pool listed here but absent from `zpool list` as
# homelab_zpool_healthy=0 / homelab_zpool_state_info{state="MISSING"}, so a pool
# that fails to import is alertable instead of silently dropping its series.
#
# Source: pve-exporters.zfs_expected_pools in hosts.conf.
{% for pool in ZFS_EXPECTED_POOLS %}
{{ pool }}
{% endfor %}
