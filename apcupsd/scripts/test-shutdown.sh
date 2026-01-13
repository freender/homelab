#!/bin/bash
# test-shutdown.sh - Dry-run test of cluster shutdown sequence
# Does NOT actually shutdown anything - just logs what would happen

echo "=== DRY-RUN: Cluster Shutdown Test ==="
echo "This script simulates the shutdown sequence without executing it."
echo ""

echo "1. Discovering running VMs cluster-wide..."
echo "   ace VMs:"
ssh ace "qm list 2>/dev/null | grep running" || echo "   (none)"
echo "   bray VMs:"
ssh bray "qm list 2>/dev/null | grep running" || echo "   (none)"
echo "   clovis VMs:"
ssh clovis "qm list 2>/dev/null | grep running" || echo "   (none)"
echo ""

echo "2. Would execute on ace:"
echo "   - qm shutdown <each running VMID> --timeout 120"
echo "   - ssh bray 'qm shutdown <vmids>'"
echo "   - ssh clovis 'qm shutdown <vmids>'"
echo "   - sleep 180 (wait for VMs)"
echo ""

echo "3. Would execute staggered host shutdowns:"
echo "   - ssh clovis 'shutdown -h +1' (1 minute delay)"
echo "   - ssh bray 'shutdown -h +2' (2 minute delay)"
echo "   - shutdown -h +3 on ace (3 minute delay)"
echo ""

echo "4. Testing SSH connectivity:"
echo -n "   ace -> bray: "
ssh ace "ssh -o ConnectTimeout=5 bray 'echo OK'" 2>/dev/null || echo "FAILED"
echo -n "   ace -> clovis: "
ssh ace "ssh -o ConnectTimeout=5 clovis 'echo OK'" 2>/dev/null || echo "FAILED"
echo ""

echo "5. Testing Telegram notification:"
echo "   Sending test message..."
ssh ace "/etc/apcupsd/telegram/telegram.sh -s 'TEST' -d 'Shutdown test from ace (dry-run)'"
echo ""

echo "=== DRY-RUN Complete ==="
echo "To perform actual shutdown test, unplug the UPS from wall power."
