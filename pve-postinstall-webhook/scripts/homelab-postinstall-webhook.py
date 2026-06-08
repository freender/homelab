#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILE = Path("/etc/homelab-postinstall-webhook/env")
EVENT_DIR = Path("/var/lib/homelab-postinstall-webhook/events")
MAX_BODY_BYTES = 1024 * 1024


def log(message: str) -> None:
    print(f"homelab-postinstall-webhook: {message}", flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            parsed = shlex.split(value, posix=True)
            result[key.strip()] = parsed[0] if parsed else ""
        except ValueError:
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


ENV = {**load_env_file(CONFIG_FILE), **os.environ}
REPO_DIR = Path(ENV.get("REPO_DIR", "/root/homelab"))
WEBHOOK_TOKEN = ENV.get("WEBHOOK_TOKEN", "")


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
    hosts_path = REPO_DIR / "hosts.conf"
    with hosts_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{hosts_path} must contain a mapping")
    return data


def payload_network_interfaces(payload: dict[str, Any]) -> list[dict[str, Any]]:
    interfaces = payload.get("network-interfaces") or payload.get("network_interfaces") or []
    return interfaces if isinstance(interfaces, list) else []


def match_host(payload: dict[str, Any]) -> str:
    hosts = load_hosts()
    fqdn = str(payload.get("fqdn") or "")
    dmi_uuid = str(nested(payload, "dmi", "system", "uuid") or "").lower()
    payload_macs = {
        normalize_mac(str(nic.get("mac") or ""))
        for nic in payload_network_interfaces(payload)
        if isinstance(nic, dict) and nic.get("mac")
    }

    for host, config in hosts.items():
        if not isinstance(config, dict) or not feature_enabled(config, "pve-autoinstall"):
            continue
        pve_autoinstall = nested(config, "features", "pve-autoinstall") or {}
        if not isinstance(pve_autoinstall, dict) or pve_autoinstall.get("pdm_host"):
            continue
        configured_fqdn = str(nested(config, "config", "hostname") or "")
        if fqdn and fqdn in {host, configured_fqdn}:
            return str(host)
        configured_uuid = str(pve_autoinstall.get("dmi_uuid") or "").lower()
        if dmi_uuid and configured_uuid and dmi_uuid == configured_uuid:
            return str(host)
        configured_mac = normalize_mac(str(pve_autoinstall.get("mgmt_mac") or ""))
        if configured_mac and configured_mac in payload_macs:
            return str(host)

    raise ValueError("webhook payload did not match any pve-autoinstall host in hosts.conf")


def enqueue_deploy(host: str, payload: dict[str, Any]) -> str:
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    safe_host = re.sub(r"[^A-Za-z0-9_.-]", "_", host)
    stamp = time.strftime("%Y%m%d%H%M%S")
    event_path = EVENT_DIR / f"{stamp}-{safe_host}.json"
    sanitized = dict(payload)
    sanitized.pop("token", None)
    event_path.write_text(json.dumps(sanitized, indent=2) + "\n", encoding="utf-8")
    event_path.chmod(0o600)

    unit = f"homelab-postinstall-deploy-{safe_host}-{stamp}"
    command = [
        "systemd-run",
        "--unit",
        unit,
        "--collect",
        "/usr/local/sbin/homelab-postinstall-deploy",
        host,
        str(event_path),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "systemd-run failed")
    return unit


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/pve-installed":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "invalid content length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "invalid body size"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "json body must be an object"})
            return
        if not WEBHOOK_TOKEN or payload.get("token") != WEBHOOK_TOKEN:
            self.send_json(403, {"error": "forbidden"})
            return

        try:
            host = match_host(payload)
            unit = enqueue_deploy(host, payload)
        except Exception as exc:
            log(f"request failed: {exc}")
            self.send_json(500, {"error": str(exc)})
            return

        log(f"queued deploy for {host} as {unit}")
        self.send_json(202, {"status": "queued", "host": host, "unit": unit})

    def log_message(self, fmt: str, *args: object) -> None:
        log(fmt % args)

    def send_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = (json.dumps(body) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    host = ENV.get("LISTEN_HOST", "0.0.0.0")
    port = int(ENV.get("LISTEN_PORT", "9443"))
    if not WEBHOOK_TOKEN:
        raise SystemExit("WEBHOOK_TOKEN is empty")
    log(f"listening on {host}:{port}, repo={REPO_DIR}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
