#!/bin/bash
# test-shutdown.sh - Dry-run inspection of the cluster shutdown sequence.
# Does NOT shut anything down; it reports what the deployed doshutdown would do.
#
# Roles come from hosts.conf: bray is the UPS master for the ace/bray/clovis
# cluster, osiris is a standalone master. Slaves are powered off by the master
# over SSH, so master->slave SSH is the load-bearing dependency here.

set -u

MASTER="bray"
SLAVES=("ace" "clovis")

echo "=== DRY-RUN: Cluster Shutdown Inspection ==="
echo "Master: $MASTER   Slaves: ${SLAVES[*]}"
echo ""

echo "1. Running VMs cluster-wide:"
for NODE in "$MASTER" "${SLAVES[@]}"; do
    echo "   $NODE:"
    ssh "$NODE" "qm list 2>/dev/null | awk '\$3==\"running\"'" 2>/dev/null | sed 's/^/     /' || echo "     (unreachable)"
done
echo ""

echo "2. Deployed doshutdown on $MASTER:"
if ssh "$MASTER" "test -x /etc/apcupsd/doshutdown" 2>/dev/null; then
    echo "   present and executable"
    ssh "$MASTER" "bash -n /etc/apcupsd/doshutdown" 2>/dev/null \
        && echo "   syntax OK" || echo "   SYNTAX ERROR"
    echo "   phases:"
    ssh "$MASTER" "grep -E '^\\\$LOGGER \"PHASE' /etc/apcupsd/doshutdown" 2>/dev/null | sed 's/^/     /'
else
    echo "   MISSING or not executable"
fi
echo ""

echo "3. Master -> slave SSH (used to power off slaves in PHASE 3):"
for NODE in "${SLAVES[@]}"; do
    echo -n "   $MASTER -> $NODE: "
    ssh "$MASTER" "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no $NODE 'echo OK'" 2>/dev/null || echo "FAILED"
done
echo ""

echo "4. UPS state as the alerting stack sees it:"
for NODE in "$MASTER" "osiris"; do
    echo -n "   $NODE: "
    ssh "$NODE" "curl -s --max-time 5 localhost:9162/metrics | grep -E '^apcupsd_(status|time_left)' | tr '\n' ' '" 2>/dev/null || echo "exporter unreachable"
    echo ""
done
echo ""

echo "=== DRY-RUN Complete ==="
echo "UPS alerting is handled by the vmalert 'ups' group on helm; this module no"
echo "longer sends Telegram messages itself."
echo "For a real test, pull mains power from the UPS."
