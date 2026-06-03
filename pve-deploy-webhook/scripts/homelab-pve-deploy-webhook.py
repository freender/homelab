#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TOKEN = os.environ.get("PVE_DEPLOY_WEBHOOK_TOKEN", "")
LISTEN_HOST = os.environ.get("PVE_DEPLOY_WEBHOOK_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PVE_DEPLOY_WEBHOOK_PORT", "8088"))
ALLOWED_HOSTS = set(os.environ.get("PVE_DEPLOY_WEBHOOK_ALLOWED_HOSTS", "").split())
HOMELAB_ROOT = Path(os.environ.get("HOMELAB_ROOT", "/home/freender/homelab"))
DEPLOY = HOMELAB_ROOT / "deploy"
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


def log(message: str) -> None:
    print(f"homelab-pve-deploy-webhook: {message}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_text(200, "ok\n")
            return
        self.send_text(404, "not found\n")

    def do_POST(self) -> None:
        if self.path != "/pve-postinstall":
            self.send_text(404, "not found\n")
            return

        payload = self.read_json()
        if payload is None:
            self.send_text(400, "invalid json\n")
            return

        if not TOKEN or payload.get("token") != TOKEN:
            self.send_text(403, "forbidden\n")
            return

        host = self.host_from_payload(payload)
        if not host:
            self.send_text(400, "unable to identify allowed host\n")
            return

        log(f"received post-install callback for {host}")
        result = subprocess.run(
            [str(DEPLOY), "pve-postinstall", host],
            cwd=str(HOMELAB_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=1800,
            check=False,
        )
        sys.stdout.write(result.stdout)
        sys.stdout.flush()

        if result.returncode == 0:
            self.send_text(200, f"deployed {host}\n")
        else:
            self.send_text(500, f"deploy failed for {host}\n")

    def read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                return None
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def host_from_payload(self, payload: dict) -> str | None:
        fqdn = str(payload.get("fqdn", "")).strip()
        host = fqdn.split(".", 1)[0] if fqdn else ""
        if not HOST_RE.match(host):
            return None
        if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
            return None
        return host

    def send_text(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: object) -> None:
        log(fmt % args)


def main() -> None:
    if not TOKEN:
        raise SystemExit("PVE_DEPLOY_WEBHOOK_TOKEN is required")
    if not DEPLOY.is_file():
        raise SystemExit(f"deploy wrapper not found: {DEPLOY}")
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log(f"listening on {LISTEN_HOST}:{LISTEN_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
