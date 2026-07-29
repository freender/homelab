#!/bin/bash
# test-shutdown.sh - Dry-run inspection of the cluster shutdown sequence.
# Does NOT shut anything down; it reports what the deployed doshutdown would do.
#
# Roles come from hosts.conf: bray is the UPS master for the ace/bray/clovis
# cluster, osiris is a standalone master (own UPS, no slaves). Cluster slaves
# are powered off by bray over SSH, so master->slave SSH is a load-bearing
# dependency there; osiris has no such dependency, only local guest drain.

set -u

MASTER="bray"
SLAVES=("ace" "clovis")
STANDALONE="osiris"

echo "=== DRY-RUN: Cluster Shutdown Inspection ==="
echo "Cluster master: $MASTER   Slaves: ${SLAVES[*]}   Standalone: $STANDALONE"
echo ""

echo "1. Running VMs and containers on all UPS-managed hosts:"
for NODE in "$MASTER" "${SLAVES[@]}" "$STANDALONE"; do
    echo "   $NODE:"
    ssh "$NODE" "qm list 2>/dev/null | awk '\$3==\"running\"'" 2>/dev/null | sed 's/^/     /' || echo "     (unreachable)"
    ssh "$NODE" "pct list 2>/dev/null | awk '\$2==\"running\"'" 2>/dev/null | sed 's/^/     /' || echo "     (unreachable)"
done
echo ""

echo "2. Deployed doshutdown scripts:"
for NODE in "$MASTER" "$STANDALONE"; do
    echo "   $NODE:"
    if ssh "$NODE" "test -x /etc/apcupsd/doshutdown" 2>/dev/null; then
        echo "     present and executable"
        ssh "$NODE" "bash -n /etc/apcupsd/doshutdown" 2>/dev/null \
            && echo "     syntax OK" || echo "     SYNTAX ERROR"
        echo "     phases:"
        ssh "$NODE" "grep -E '^\\\$LOGGER \"PHASE' /etc/apcupsd/doshutdown" 2>/dev/null | sed 's/^/       /'
    else
        echo "     MISSING or not executable"
    fi
done
echo ""

echo "3. Master -> slave SSH (used to power off slaves in PHASE 3):"
for NODE in "${SLAVES[@]}"; do
    echo -n "   $MASTER -> $NODE: "
    ssh "$MASTER" "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no $NODE 'echo OK'" 2>/dev/null || echo "FAILED"
done
echo ""

echo "4. UPS state as the alerting stack sees it:"
for NODE in "$MASTER" "$STANDALONE"; do
    echo -n "   $NODE: "
    ssh "$NODE" "curl -s --max-time 5 localhost:9162/metrics | grep -E '^apcupsd_(status|time_left)' | tr '\n' ' '" 2>/dev/null || echo "exporter unreachable"
    echo ""
done
echo ""

echo "=== DRY-RUN Complete ==="
echo "UPS alerting is handled by the vmalert 'ups' group on helm; this module no"
echo "longer sends Telegram messages itself."
echo "For a real test, pull mains power from the UPS."
