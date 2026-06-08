#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILE = Path("/etc/homelab-postinstall-webhook/env")


def log(message: str) -> None:
    print(f"homelab-pdm-refresh-remote: {message}", flush=True)


def fail(message: str) -> None:
    print(f"homelab-pdm-refresh-remote: ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


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
PDM_TOKEN_SECRET = ENV.get("PDM_TOKEN_SECRET", "")
PDM_TOKEN_REF = ENV.get("PDM_TOKEN_REF", "op://Homelab/PDM Deploy API Token/password")
OP_BIN = ENV.get("OP_BIN", "/root/.local/bin/op")
OP_TOKEN_FILE = ENV.get(
    "OP_SERVICE_ACCOUNT_TOKEN_FILE",
    "/root/.config/op/service-account-token",
)
SSH_AUTH_SOCK = ENV.get("SSH_AUTH_SOCK", "/root/.ssh/agent.sock")
PDM_REMOTE_REFRESH = ENV.get("PDM_REMOTE_REFRESH", "true").lower() in {
    "1",
    "true",
    "yes",
}


class PdmApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"PDM API HTTP {status}: {message}")


def op_read(ref: str) -> str:
    env = os.environ.copy()
    if "OP_SERVICE_ACCOUNT_TOKEN" not in env:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = (
            Path(OP_TOKEN_FILE).read_text(encoding="utf-8").strip()
        )
    result = subprocess.run(
        [OP_BIN, "read", ref],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def pdm_request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    token = PDM_TOKEN_SECRET or op_read(PDM_TOKEN_REF)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{PDM_BASE_URL}{path}", data=data, method=method)
    req.add_header("Authorization", f"PDMAPIToken {PDM_TOKEN_ID}:{token}")
    req.add_header("Content-Type", "application/json")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            payload = json.loads(resp.read() or b"{}")
            return payload.get("data")
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        try:
            payload = json.loads(body_bytes or b"{}")
            message = str(payload.get("errors") or payload.get("message") or payload)
        except Exception:
            message = body_bytes.decode(errors="replace")
        raise PdmApiError(exc.code, message) from exc


def nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def feature_enabled(config: dict[str, Any], feature: str) -> bool:
    features = config.get("features") or {}
    if not isinstance(features, dict) or feature not in features:
        return False
    value = features[feature]
    return not (
        value is False or (isinstance(value, dict) and value.get("enabled") is False)
    )


def load_host(host: str) -> dict[str, Any]:
    data = yaml.safe_load((REPO_DIR / "hosts.conf").read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or host not in data or not isinstance(data[host], dict):
        fail(f"host {host!r} not found in hosts.conf")
    return data[host]


def ssh_target(config: dict[str, Any]) -> str:
    host_config = config.get("config") or {}
    hostname = str(host_config.get("hostname") or "")
    user = str(host_config.get("user") or "root")
    if not hostname:
        fail("config.hostname is required")
    return f"{user}@{hostname}"


def run_ssh(target: str, remote_args: list[str]) -> str:
    env = os.environ.copy()
    env["SSH_AUTH_SOCK"] = SSH_AUTH_SOCK
    remote_command = shlex.join(remote_args)
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/root/.ssh/known_hosts",
        target,
        remote_command,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


def parse_authid(authid: str) -> tuple[str, str]:
    user, sep, token_name = authid.partition("!")
    if not sep or not user or not token_name:
        fail(f"remote authid must be a PVE API token id, got {authid!r}")
    return user, token_name


def read_pve_fingerprint(target: str) -> str:
    output = run_ssh(
        target,
        [
            "openssl",
            "x509",
            "-in",
            "/etc/pve/local/pve-ssl.pem",
            "-noout",
            "-fingerprint",
            "-sha256",
        ],
    )
    match = re.search(r"Fingerprint=([0-9A-Fa-f:]+)", output)
    if not match:
        fail("could not parse PVE certificate fingerprint")
    return match.group(1).upper()


def remote_version_ok(remote_id: str) -> bool:
    try:
        pdm_request("GET", f"/api2/json/remotes/remote/{remote_id}/version")
        return True
    except PdmApiError as exc:
        log(f"remote version check failed: HTTP {exc.status}: {exc.message}")
        return False


def update_pdm_remote(
    remote_id: str,
    node: str,
    fingerprint: str,
    authid: str | None = None,
    token: str | None = None,
) -> None:
    body: dict[str, Any] = {
        "nodes": [f"{node},fingerprint={fingerprint}"],
    }
    if authid is not None:
        body["authid"] = authid
    if token is not None:
        body["token"] = token
    pdm_request("PUT", f"/api2/json/remotes/remote/{remote_id}", body)


def rotate_pve_token(target: str, authid: str, comment: str) -> str:
    user, token_name = parse_authid(authid)
    maybe_remove_pve_token(target, authid)
    # Keep the token secret only in this process and the PDM config.
    try:
        output = run_ssh(
            target,
            [
                "pveum",
                "user",
                "token",
                "add",
                user,
                token_name,
                "--privsep",
                "0",
                "--expire",
                "0",
                "--comment",
                comment,
                "--output-format",
                "json",
            ],
        )
    except subprocess.CalledProcessError as exc:
        fail(f"failed to create PVE API token on remote: {exc.stderr.strip()}")
    try:
        payload = json.loads(output)
        value = str(payload.get("value") or "").strip()
    except Exception as exc:
        fail(f"could not parse pveum token JSON: {exc}")
    if not value:
        fail("pveum token add did not return a token value")
    return value


def maybe_remove_pve_token(target: str, authid: str) -> None:
    user, token_name = parse_authid(authid)
    try:
        run_ssh(target, ["pveum", "user", "token", "remove", user, token_name])
    except subprocess.CalledProcessError:
        pass


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: homelab-pdm-refresh-remote <host>")
    host = sys.argv[1]
    if not PDM_REMOTE_REFRESH:
        log("disabled by PDM_REMOTE_REFRESH")
        return

    config = load_host(host)
    host_config = config.get("config") or {}
    if host_config.get("type") != "pve" or host_config.get("standalone") is not True:
        log(f"skipping {host}: not a standalone PVE host")
        return
    if not feature_enabled(config, "pve-autoinstall"):
        log(f"skipping {host}: pve-autoinstall not enabled")
        return

    pve_auto = nested(config, "features", "pve-autoinstall") or {}
    if not isinstance(pve_auto, dict) or pve_auto.get("pdm_host"):
        log(f"skipping {host}: host is the PDM server entry")
        return

    remote_id = str(pve_auto.get("pdm_remote_id") or host)
    node = str(pve_auto.get("pdm_remote_node") or host_config.get("hostname") or "")
    authid = str(
        pve_auto.get("pdm_remote_authid")
        or ENV.get("PDM_REMOTE_AUTHID", "root@pam!pdm-rasputin")
    )
    comment = str(
        pve_auto.get("pdm_remote_token_comment")
        or ENV.get("PDM_REMOTE_TOKEN_COMMENT", "PDM on arc")
    )
    if not node:
        fail("PDM remote node hostname is empty")

    target = ssh_target(config)
    log(f"refreshing PDM remote {remote_id} for node {node}")
    fingerprint = read_pve_fingerprint(target)
    log(f"live fingerprint: {fingerprint}")

    log("updating PDM remote fingerprint")
    update_pdm_remote(remote_id, node, fingerprint)
    if remote_version_ok(remote_id):
        log("PDM remote is authorized after fingerprint refresh")
        return

    log("rotating rebuilt-node PVE API token")
    token = rotate_pve_token(target, authid, comment)
    log("updating PDM remote token and fingerprint")
    update_pdm_remote(remote_id, node, fingerprint, authid=authid, token=token)

    if not remote_version_ok(remote_id):
        fail("PDM remote still failed after token refresh")
    log("PDM remote credential refreshed")


if __name__ == "__main__":
    main()
