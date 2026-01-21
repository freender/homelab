#!/usr/bin/env python3
import json
import os
import subprocess
import sys

DEVICE = "/dev/disk/by-id/ata-Samsung_SSD_860_EVO_M.2_250GB_S5GFNJ0N903632M"
SMARTCTL = "/usr/sbin/smartctl"
SUDO = "/usr/bin/sudo"
TYPE_OVERRIDE = "sntrealtek"

cmd = [SUDO, "-n", SMARTCTL, "--json", "--all", "-d", "sat", DEVICE]
proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
if proc.returncode != 0:
    sys.exit(0)

try:
    data = json.loads(proc.stdout)
except json.JSONDecodeError:
    sys.exit(0)

temp = None
temp_info = data.get("temperature")
if isinstance(temp_info, dict):
    temp = temp_info.get("current")
elif isinstance(temp_info, (int, float)):
    temp = temp_info

if temp is None:
    attrs = data.get("ata_smart_attributes", dict()).get("table", [])
    for attr in attrs:
        name = attr.get("name", "")
        if "Temperature" in name:
            raw = attr.get("raw", dict()).get("value")
            if raw is not None:
                temp = raw
                break

if temp is None:
    sys.exit(0)

model = data.get("model_name", "")
serial = data.get("serial_number", "")
name_tag = os.path.realpath(DEVICE)

def esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

tags = [
    ("model", model),
    ("name", name_tag),
    ("serial", serial),
    ("type", TYPE_OVERRIDE),
]

parts = [key + "=" + esc(value) for key, value in tags if value]
if not parts:
    sys.exit(0)

try:
    temp_value = int(temp)
except (TypeError, ValueError):
    sys.exit(0)

line = "smartctl," + ",".join(parts) + " temperature=" + str(temp_value) + "i"
print(line)
