#!/bin/sh
# Managed by homelab (metrics-exporters). Deployed only to hosts setting
# metrics-exporters.smartctl_wrapper: true, and used as smartctl_exporter's
# --smartctl.path.
#
# Works around two quirks of USB-attached NVMe disks behind ASMedia bridges
# (cottonwood's /dev/sda and /dev/sdb):
#
#  1. `smartctl --scan` does not report them at all, so the exporter would never
#     probe them. We append them to the scan JSON with type sntasmedia, which is
#     the device type that does work for these bridges.
#  2. Probing them returns exit_status 4 with "Read 1 entries from Error
#     Information Log failed" even though the SMART data itself is fine. The
#     exporter treats a non-zero exit as a failed device and drops it, so that
#     one benign error is downgraded to a warning and the exit status to 0.
#
# Everything else is passed through untouched, including the real exit code.
REAL=/usr/sbin/smartctl
is_json=0
is_sdb=0
is_sda=0
is_asmedia=0
is_scan=0
for arg in "$@"; do
  [ "$arg" = "--json" ] && is_json=1
  [ "$arg" = "/dev/sdb" ] && is_sdb=1
  [ "$arg" = "/dev/sda" ] && is_sda=1
  [ "$arg" = "--device=sntasmedia" ] && is_asmedia=1
  [ "$arg" = "--scan" ] && is_scan=1
  [ "$arg" = "--scan-open" ] && is_scan=1
done
out="$($REAL "$@" 2>&1)"
rc=$?
if [ "$is_json" -eq 1 ] && [ "$is_scan" -eq 1 ] && [ "$rc" -eq 0 ] && [ -b /dev/sda ] && ! printf "%s" "$out" | grep -Fq "\"name\": \"/dev/sda\""; then
  out=$(printf "%s\n" "$out" | sed "/\"devices\": \[/a\\
    {\\
      \"name\": \"/dev/sda\",\\
      \"info_name\": \"/dev/sda [USB NVMe ASMedia]\",\\
      \"type\": \"sntasmedia\",\\
      \"protocol\": \"NVMe\"\\
    },")
fi
if [ "$is_json" -eq 1 ] && [ "$is_scan" -eq 1 ] && [ "$rc" -eq 0 ] && [ -b /dev/sdb ] && ! printf "%s" "$out" | grep -Fq "\"name\": \"/dev/sdb\""; then
  out=$(printf "%s\n" "$out" | sed "/\"devices\": \[/a\\
    {\\
      \"name\": \"/dev/sdb\",\\
      \"info_name\": \"/dev/sdb [USB NVMe ASMedia]\",\\
      \"type\": \"sntasmedia\",\\
      \"protocol\": \"NVMe\"\\
    },")
fi
if [ "$is_json" -eq 1 ] && [ "$is_asmedia" -eq 1 ] && { [ "$is_sdb" -eq 1 ] || [ "$is_sda" -eq 1 ]; } && [ "$rc" -eq 4 ] && printf "%s" "$out" | grep -Fq "Read 1 entries from Error Information Log failed"; then
  printf "%s\n" "$out" \
    | sed -E "s/\"severity\":[[:space:]]*\"error\"/\"severity\": \"warn\"/" \
    | sed -E "s/\"exit_status\":[[:space:]]*4/\"exit_status\": 0/"
  exit 0
fi
printf "%s\n" "$out"
exit "$rc"
