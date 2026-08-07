# Shared HA coordination helpers for the cluster UPS handlers.

disarm_ha() {
    if ha-manager status | grep -q '^fencing armed'; then
        logger -t apcupsd-shutdown "Disarming HA while preserving resource desired states"
        if ha-manager crm-command disarm-ha ignore; then
            return 0
        fi
        # Another UPS listener may have issued the same cluster-wide command.
        if ! ha-manager status | grep -q '^fencing armed'; then
            logger -t apcupsd-shutdown "HA was disarmed by another cluster node"
            return 0
        fi
        return 1
    else
        logger -t apcupsd-shutdown "HA is already disarmed"
    fi
}
