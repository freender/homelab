#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILE = Path("/etc/homelab-postinstall-webhook/env")
STATE_DIR = Path("/var/lib/homelab-postinstall-webhook/state")
EVENT_DIR = Path("/var/lib/homelab-postinstall-webhook/events")


def log(message: str) -> None:
    print(f"homelab-pdm-installation-watch: {message}", flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed = shlex.split(value, posix=True)
        result[key.strip()] = parsed[0] if parsed else ""
    return result


ENV = {**load_env_file(CONFIG_FILE), **os.environ}
REPO_DIR = Path(ENV.get("REPO_DIR", "/root/homelab"))
PDM_BASE_URL = ENV.get("PDM_BASE_URL", "https://127.0.0.1:8443").rstrip("/")
PDM_TOKEN_ID = ENV.get("PDM_TOKEN_ID", "root@pam!homelab-deploy")
PDM_TOKEN_REF = ENV.get("PDM_TOKEN_REF", "op://Homelab/PDM Deploy API Token/password")
OP_BIN = ENV.get("OP_BIN", "/root/.local/bin/op")
OP_TOKEN_FILE = ENV.get("OP_SERVICE_ACCOUNT_TOKEN_FILE", "/root/.config/op/service-account-token")


def op_read(ref: str) -> str:
    env = os.environ.copy()
    if "OP_SERVICE_ACCOUNT_TOKEN" not in env:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = Path(OP_TOKEN_FILE).read_text(encoding="utf-8").strip()
    result = subprocess.run(
        [OP_BIN, "read", ref],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def pdm_get(path: str) -> Any:
    token = op_read(PDM_TOKEN_REF)
    req = urllib.request.Request(f"{PDM_BASE_URL}{path}")
    req.add_header("Authorization", f"PDMAPIToken {PDM_TOKEN_ID}:{token}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read())["data"]


def nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def normalize_mac(value: str) -> str:
    return re.sub(r"[^0-9a-f]", "", value.lower())


def feature_enabled(config: dict[str, Any], feature: str) -> bool:
    features = config.get("features") or {}
    if not isinstance(features, dict) or feature not in features:
        return False
    value = features[feature]
    return not (value is False or (isinstance(value, dict) and value.get("enabled") is False))


def load_hosts() -> dict[str, Any]:
    data = yaml.safe_load((REPO_DIR / "hosts.conf").read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("hosts.conf must contain a mapping")
    return data


def installation_macs(install: dict[str, Any]) -> set[str]:
    interfaces = nested(install, "info", "network_interfaces") or []
    if not isinstance(interfaces, list):
        return set()
    return {
        normalize_mac(str(nic.get("mac") or ""))
        for nic in interfaces
        if isinstance(nic, dict) and nic.get("mac")
    }


def match_host(install: dict[str, Any]) -> str:
    answer_id = str(install.get("answer-id") or "")
    dmi_uuid = str(nested(install, "info", "dmi", "system", "uuid") or "").lower()
    macs = installation_macs(install)
    hosts = load_hosts()

    for host, config in hosts.items():
        if not isinstance(config, dict) or not feature_enabled(config, "pve-autoinstall"):
            continue
        pve_autoinstall = nested(config, "features", "pve-autoinstall") or {}
        if not isinstance(pve_autoinstall, dict) or pve_autoinstall.get("pdm_host"):
            continue
        if answer_id and answer_id == str(pve_autoinstall.get("answer_name") or ""):
            return str(host)
        configured_uuid = str(pve_autoinstall.get("dmi_uuid") or "").lower()
        if dmi_uuid and configured_uuid and dmi_uuid == configured_uuid:
            return str(host)
        configured_mac = normalize_mac(str(pve_autoinstall.get("mgmt_mac") or ""))
        if configured_mac and configured_mac in macs:
            return str(host)

    raise ValueError(f"installation {install.get('uuid')} did not match hosts.conf")


def queue_deploy(host: str, install: dict[str, Any]) -> None:
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    uuid = str(install["uuid"])
    safe_host = re.sub(r"[^A-Za-z0-9_.-]", "_", host)
    stamp = time.strftime("%Y%m%d%H%M%S")
    event_path = EVENT_DIR / f"{stamp}-{safe_host}-{uuid}.json"
    state_path = STATE_DIR / f"{uuid}.queued"
    if state_path.exists():
        return
    event_path.write_text(json.dumps(install, indent=2) + "\n", encoding="utf-8")
    event_path.chmod(0o600)
    unit = f"homelab-postinstall-deploy-{safe_host}-{stamp}"
    subprocess.run(
        [
            "systemd-run",
            "--unit",
            unit,
            "--collect",
            "/usr/local/sbin/homelab-postinstall-deploy",
            host,
            str(event_path),
        ],
        check=True,
    )
    state_path.write_text(f"{host}\n{unit}\n", encoding="utf-8")
    log(f"queued deploy for {host} from PDM installation {uuid} as {unit}")


def main() -> None:
    installs = pdm_get("/api2/json/auto-install/installations")
    if not isinstance(installs, list):
        raise SystemExit("PDM installations response is not a list")
    queued = 0
    for install in installs:
        if not isinstance(install, dict) or install.get("status") != "finished":
            continue
        uuid = install.get("uuid")
        if not uuid or (STATE_DIR / f"{uuid}.queued").exists():
            continue
        host = match_host(install)
        queue_deploy(host, install)
        queued += 1
    log(f"scan complete; queued={queued}")


if __name__ == "__main__":
    main()
