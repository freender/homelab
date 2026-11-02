"""pbs-remote-textfile-exporter: the offsite add-only check run from xur.

What is pinned here is what decides whether PbsRemoteExcessPrivileges can fire
and whether a credential can leak: the fingerprint is checked before anything
is sent, a privilege outside the allowed set is published, and one failing
remote neither hides the others nor reads as "no excess".
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "pbs-remote-textfile-exporter.py"

CERT = b"remote-certificate"
FINGERPRINT = ":".join(f"{b:02X}" for b in hashlib.sha256(CERT).digest())


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


REMOTE_CFG = f"""remote: zavala-push
\tauth-id xur-sync@pbs
\tcomment Cinci PBS push
\tfingerprint {FINGERPRINT}
\thost cinci.internal
\tpassword {b64("hunter2")}

remote: zavala-pull
\tauth-id xur-pull@pbs!pull
\tfingerprint {FINGERPRINT.lower()}
\thost cinci.internal
\tport 8008
\tpassword {b64("token-secret")}
"""

ADD_ONLY = {
    "/datastore/backup": {
        "Datastore.Audit": True,
        "Datastore.Backup": True,
        "Datastore.Read": True,
        "Datastore.Verify": True,
    }
}


def _load():
    spec = importlib.util.spec_from_file_location("pbs_remote_textfile_exporter", EXPORTER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the defining module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter():
    return _load()


class FakeSock:
    def __init__(self, cert: bytes) -> None:
        self.cert = cert

    def getpeercert(self, binary_form: bool = False) -> bytes:
        return self.cert


class FakeResponse:
    def __init__(self, status: int, data: object) -> None:
        self.status = status
        self.data = data

    def read(self) -> bytes:
        return json.dumps({"data": self.data}).encode()


class FakeServer:
    """Answers like a remote PBS and records every request it was sent."""

    def __init__(self, permissions: object = ADD_ONLY, cert: bytes = CERT) -> None:
        self.permissions = permissions
        self.cert = cert
        self.sent: list[tuple[str, str, bytes | None, dict]] = []
        self.connects: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int, **_: object) -> FakeServer.Conn:
        self.connects.append((host, port))
        return FakeServer.Conn(self)

    class Conn:
        def __init__(self, server: FakeServer) -> None:
            self.server = server
            self.sock = None

        def connect(self) -> None:
            self.sock = FakeSock(self.server.cert)

        def request(self, method, url, body=None, headers=None) -> None:
            self.server.sent.append((method, url, body, dict(headers or {})))

        def getresponse(self) -> FakeResponse:
            method, url, _body, _headers = self.server.sent[-1]
            if url.endswith("/access/ticket"):
                return FakeResponse(200, {"ticket": "PBS:ticket"})
            return FakeResponse(200, self.server.permissions)

        def close(self) -> None:
            pass


@pytest.fixture
def server(exporter, monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(exporter, "_connection", fake)
    return fake


def series(text: str) -> dict[str, str]:
    return {
        line.rpartition(" ")[0]: line.rpartition(" ")[2]
        for line in text.splitlines()
        if line and not line.startswith("#")
    }


def test_parses_remote_cfg(exporter) -> None:
    push, pull = exporter.parse_remotes(REMOTE_CFG)

    assert (push.name, push.host, push.port, push.auth_id) == (
        "zavala-push",
        "cinci.internal",
        8007,
        "xur-sync@pbs",
    )
    assert push.fingerprint == hashlib.sha256(CERT).hexdigest()
    assert pull.fingerprint == push.fingerprint
    assert pull.port == 8008
    assert exporter.secret(push) == "hunter2"


def test_user_logs_in_for_a_ticket_then_reads_its_permissions(exporter, server) -> None:
    push = exporter.parse_remotes(REMOTE_CFG)[0]

    assert exporter.check(push) == []
    (login_method, login_url, login_body, _), (method, url, _, headers) = server.sent
    assert (login_method, login_url) == ("POST", "/api2/json/access/ticket")
    assert b"username=xur-sync%40pbs" in login_body and b"password=hunter2" in login_body
    assert (method, url) == ("GET", "/api2/json/access/permissions")
    assert headers == {"Cookie": "PBSAuthCookie=PBS%3Aticket"}


def test_token_authenticates_by_header_without_a_ticket(exporter, server) -> None:
    pull = exporter.parse_remotes(REMOTE_CFG)[1]

    exporter.check(pull)
    assert [sent[1] for sent in server.sent] == ["/api2/json/access/permissions"]
    assert server.sent[0][3] == {"Authorization": "PBSAPIToken=xur-pull@pbs!pull:token-secret"}
    assert server.connects == [("cinci.internal", 8008)]


def test_fingerprint_mismatch_sends_nothing(exporter, server) -> None:
    server.cert = b"impostor"
    push = exporter.parse_remotes(REMOTE_CFG)[0]

    with pytest.raises(exporter.RemoteError, match="fingerprint mismatch"):
        exporter.check(push)
    assert server.sent == []


def test_unpinned_remote_is_refused_before_connecting(exporter, server) -> None:
    remote = exporter.parse_remotes(REMOTE_CFG.replace(f"\tfingerprint {FINGERPRINT}\n", ""))[0]

    with pytest.raises(exporter.RemoteError, match="no fingerprint"):
        exporter.check(remote)
    assert server.connects == []


def test_every_request_pins_its_own_connection(exporter, server) -> None:
    exporter.check(exporter.parse_remotes(REMOTE_CFG)[0])
    assert len(server.connects) == len(server.sent) == 2


EXCESS = [
    "Datastore.Prune",
    "Datastore.Modify",
    "Datastore.Allocate",
    "Sys.Modify",
    "Permissions.Modify",
]
PULL_LABELS = 'remote="zavala-pull",auth_id="xur-pull@pbs!pull"'


@pytest.mark.parametrize("privilege", EXCESS)
def test_privilege_outside_the_allowed_set_is_excess(exporter, server, privilege: str) -> None:
    server.permissions = {
        "/datastore/backup": {"Datastore.Backup": True},
        "/": {privilege: True},
    }
    assert exporter.check(exporter.parse_remotes(REMOTE_CFG)[0]) == [("/", privilege)]


def test_http_error_is_a_failed_check(exporter, server, monkeypatch) -> None:
    monkeypatch.setattr(FakeServer.Conn, "getresponse", lambda self: FakeResponse(401, None))
    with pytest.raises(exporter.RemoteError, match="HTTP 401"):
        exporter.check(exporter.parse_remotes(REMOTE_CFG)[0])


def test_connection_error_is_a_failed_check(exporter, server, monkeypatch) -> None:
    def refuse(self) -> None:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(FakeServer.Conn, "connect", refuse)
    with pytest.raises(exporter.RemoteError, match="refused"):
        exporter.check(exporter.parse_remotes(REMOTE_CFG)[0])


def test_non_base64_password_is_a_failed_check(exporter) -> None:
    remote = exporter.parse_remotes(REMOTE_CFG.replace(b64("hunter2"), "not base64!"))[0]
    with pytest.raises(exporter.RemoteError, match="base64"):
        exporter.secret(remote)


def test_render_publishes_excess_and_omits_it_for_a_failed_check(exporter) -> None:
    push, pull = exporter.parse_remotes(REMOTE_CFG)
    out = series(exporter.render([(push, [("/", "Datastore.Prune")]), (pull, None)]))
    push_labels = 'remote="zavala-push",auth_id="xur-sync@pbs"'

    assert out["homelab_pbs_remotes_total"] == "2"
    assert out[f"homelab_pbs_remote_check_ok{{{push_labels}}}"] == "1"
    assert out[f"homelab_pbs_remote_excess_privileges{{{push_labels}}}"] == "1"
    excess = (
        f'homelab_pbs_remote_excess_privilege{{{push_labels},path="/",privilege="Datastore.Prune"}}'
    )
    assert out[excess] == "1"
    assert out[f"homelab_pbs_remote_check_ok{{{PULL_LABELS}}}"] == "0"
    # A failed check must never read as "no excess".
    assert not any(
        name.startswith("homelab_pbs_remote_excess_privileges") and "zavala-pull" in name
        for name in out
    )


def test_one_failing_remote_does_not_hide_the_others(
    exporter, server, tmp_path, monkeypatch
) -> None:
    cfg = tmp_path / "remote.cfg"
    cfg.write_text(REMOTE_CFG.replace(b64("token-secret"), "not base64!"))
    monkeypatch.setattr(exporter, "REMOTE_CFG", cfg)
    monkeypatch.setattr(exporter, "OUT_DIR", tmp_path)
    monkeypatch.setattr(exporter, "OUT_FILE", tmp_path / "pbs-remotes.prom")

    assert exporter.main() == 0
    out = series((tmp_path / "pbs-remotes.prom").read_text())
    assert out['homelab_pbs_remote_check_ok{remote="zavala-push",auth_id="xur-sync@pbs"}'] == "1"
    assert out[f"homelab_pbs_remote_check_ok{{{PULL_LABELS}}}"] == "0"
    assert (tmp_path / "pbs-remotes.prom").stat().st_mode & 0o777 == 0o644


def test_unreadable_config_leaves_the_previous_file(exporter, tmp_path, monkeypatch) -> None:
    previous = tmp_path / "pbs-remotes.prom"
    previous.write_text("old\n")
    monkeypatch.setattr(exporter, "REMOTE_CFG", tmp_path / "missing.cfg")
    monkeypatch.setattr(exporter, "OUT_DIR", tmp_path)
    monkeypatch.setattr(exporter, "OUT_FILE", previous)

    assert exporter.main() == 1
    assert previous.read_text() == "old\n"


def test_labels_avoid_the_names_the_scrape_config_owns(exporter) -> None:
    push = exporter.parse_remotes(REMOTE_CFG)[0]
    text = exporter.render([(push, [("/", "Sys.Modify")])])
    assert 'host="' not in text and 'job="' not in text
