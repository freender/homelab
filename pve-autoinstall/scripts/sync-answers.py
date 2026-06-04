#!/usr/bin/env python3
"""sync-answers.py - Run on rasputin to sync PDM prepared answers from answer plan.

Reads answer-plan.json from the same directory, calls PDM API on localhost,
creates or updates each prepared answer entry. Never logs secret values.

Usage: python3 sync-answers.py [--dry-run] [--force]
"""

import ctypes
import hashlib
import json
import random
import ssl
import string
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
PLAN_FILE = SCRIPT_DIR / "answer-plan.json"
TOKEN_FILE = SCRIPT_DIR / "pdm-api-token"


def log(msg: str) -> None:
    print(f"sync-answers: {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"sync-answers: ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def hash_password(plaintext: str) -> str:
    """Hash password using SHA-512 crypt via libcrypt.so.1."""
    try:
        lib = ctypes.CDLL("libcrypt.so.1", use_errno=True)
        lib.crypt.restype = ctypes.c_char_p
        lib.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        chars = string.ascii_letters + string.digits + "./"
        salt = "".join(random.choices(chars, k=16))
        setting = f"$6${salt}".encode()
        result = lib.crypt(plaintext.encode(), setting)
        if result:
            return result.decode()
        raise RuntimeError(f"crypt() errno={ctypes.get_errno()}")
    except OSError as exc:
        fail(f"libcrypt unavailable: {exc}")


class PdmClient:
    def __init__(self, base_url: str, token_id: str, token_secret: str, fingerprint: str) -> None:
        self._base = base_url.rstrip("/")
        self._auth = f"PDMAPIToken {token_id}:{token_secret}"
        self._ctx = self._build_ctx(base_url, fingerprint)

    @staticmethod
    def _build_ctx(base_url: str, expected_fp: str) -> ssl.SSLContext:
        import socket

        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port or 8443
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = ctx.wrap_socket(
            socket.create_connection((host, port), timeout=10),
            server_hostname=host,
        )
        der = conn.getpeercert(binary_form=True)
        conn.close()
        fp = hashlib.sha256(der).hexdigest().upper()
        expected = expected_fp.upper().replace(":", "")
        if fp != expected:
            fail(f"TLS fingerprint mismatch: expected {expected}, got {fp}")
        return ctx

    def _req(self, method: str, path: str, body=None):
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            try:
                msg = json.loads(body_bytes).get("errors") or body_bytes.decode()
            except Exception:
                msg = body_bytes.decode(errors="replace")
            fail(f"PDM API {method} {path} HTTP {exc.code}: {msg}")

    def get_answer(self, name: str):
        try:
            return self._req("GET", f"/api2/json/auto-install/prepared/{name}")["data"]
        except SystemExit:
            return None

    def create(self, payload: dict) -> None:
        self._req("POST", "/api2/json/auto-install/prepared", payload)

    def update(self, name: str, payload: dict) -> None:
        self._req("PUT", f"/api2/json/auto-install/prepared/{name}", payload)


def answers_differ(existing: dict, desired: dict) -> bool:
    comparable = [
        "fqdn", "keyboard", "country", "timezone", "mailto",
        "filesystem", "disk-mode", "disk-filter", "disk-filter-match",
        "use-dhcp-network", "use-dhcp-fqdn", "cidr", "gateway", "dns",
        "netdev-filter", "authorized-tokens", "netif-name-pinning-enabled",
        "reboot-mode", "reboot-on-error", "is-default", "target-filter",
        "root-ssh-keys",
    ]
    return any(existing.get(k) != desired.get(k) for k in comparable)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    if not PLAN_FILE.is_file():
        fail(f"missing plan file: {PLAN_FILE}")
    if not TOKEN_FILE.is_file():
        fail(f"missing token file: {TOKEN_FILE}")

    plan = json.loads(PLAN_FILE.read_text())
    token_lines = TOKEN_FILE.read_text().strip().splitlines()
    env = {}
    for line in token_lines:
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()

    pdm_token_secret = env.get("PDM_DEPLOY_TOKEN", "").strip()
    if not pdm_token_secret:
        fail("PDM_DEPLOY_TOKEN empty in token file")

    pdm = plan["pdm"]
    client = PdmClient(
        base_url=f"https://{pdm['host']}:{pdm['port']}",
        token_id=pdm["token_id"],
        token_secret=pdm_token_secret,
        fingerprint=pdm["cert_fingerprint"],
    )
    log(f"PDM connection OK: {pdm['host']}:{pdm['port']}")

    if dry_run:
        log("[DRY-RUN] would sync the following answers:")
        for entry in plan["answers"]:
            log(f"  {entry['id']}: {entry['fqdn']} cidr={entry['cidr']}")
        return

    for entry in plan["answers"]:
        name = entry["id"]
        host = entry.get("_host", name)
        root_password = env.get(f"PVE_ROOT_PASSWORD__{host}", "").strip()
        if not root_password:
            fail(f"PVE_ROOT_PASSWORD__{host} empty in token file")
        pwd_hash = hash_password(root_password)
        payload = dict(entry)
        payload["root-password-hashed"] = pwd_hash
        # Remove non-PDM planning fields
        for k in ("_host",):
            payload.pop(k, None)

        existing = client.get_answer(name)
        if existing is None:
            log(f"creating answer '{name}'...")
            client.create(payload)
            log(f"  created '{name}'")
        elif force or answers_differ(existing, payload):
            reason = "forced" if force else "config changed"
            log(f"updating answer '{name}' ({reason})...")
            client.update(name, payload)
            log(f"  updated '{name}'")
        else:
            log(f"answer '{name}' up to date (use --force to re-push password hash)")

    log("sync complete")


if __name__ == "__main__":
    main()
