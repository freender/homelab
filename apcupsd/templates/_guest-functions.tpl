# Shared shell functions for VM/container shutdown coordination.
# Included (not executed standalone) by doshutdown-master.tpl,
# doshutdown-slave.tpl, and doshutdown-master-standalone.tpl so all three
# roles drain guests identically. Safe to redefine per script context (local
# shell, or a script body sent to a remote host over ssh) - each context is
# an independent bash process.

shutdown_running_guests() {
  local label="$1"
  for VMID in $(qm list 2>/dev/null | awk '$3=="running"{print $1}'); do
    logger -t apcupsd-shutdown "Shutting down VM $VMID on $label"
    qm shutdown "$VMID" --timeout 120 &
  done
  for CTID in $(pct list 2>/dev/null | awk '$2=="running"{print $1}'); do
    logger -t apcupsd-shutdown "Shutting down container $CTID on $label"
    pct shutdown "$CTID" --timeout 120 &
  done
}

list_running_guests() {
  { qm list 2>/dev/null | awk '$3=="running"{print $1}'; pct list 2>/dev/null | awk '$2=="running"{print $1}'; }
}
