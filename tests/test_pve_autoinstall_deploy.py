"""Control flow of `pve_autoinstall.deploy()` — the module's failure and skip paths.

pve-autoinstall does not use `run_module_deploy`: it drives one fixed host (the PDM
host) and hand-rolls the host resolution, validation, and secret loading that the
shared prologue would otherwise own. That makes its `deploy()` the branchiest entry
point in the repo, and every branch below returns 1 or 0 without raising — so a
regression here is silent, not a traceback.

`tests/test_dry_run_all_modules.py` only reaches the happy dry-run path. These tests
cover the rest. The stakes are why they are worth writing: the answer file this
builds drives an unattended installer that wipes the matched disk, so "refused to
proceed" is the required behavior whenever the inputs are not fully understood.

Secrets and remote execution are stubbed; what is asserted is which branch was taken,
what exit code came back, and whether a remote run was attempted at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homelab import op_secrets
from homelab.deploy import DeploySession
from homelab.modules import pve_autoinstall

PDM_BLOCK = """
    pve-autoinstall:
      pdm_host: 10.0.0.50
      pdm_port: 8443
      pdm_token_id: "root@pam!homelab-deploy"
      install_auth_token_name: homelab-pve-auto-install
      root_ssh_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAnDhQqrfeLPYGIP"
      mailto: nobody@example.net
      keyboard: en-us
      country: us
"""

NODE_BLOCK = """
    pve-autoinstall:
      dmi_uuid: 03d502e0-045e-0525-a506-e00700080009
      boot_disk_serial: SERIAL123
      answer_name: ace
      cidr: 10.0.0.11/24
      gateway: 10.0.0.1
      mgmt_mac: "aa:bb:cc:dd:ee:ff"
"""


def _hosts_conf(*, pdm_hosts: list[str], nodes: list[str]) -> str:
    chunks = []
    for name in pdm_hosts:
        chunks.append(
            f"{name}:\n"
            "  config:\n"
            "    type: lxc\n"
            f"    hostname: {name}.internal\n"
            "    user: root\n"
            "    sshkey: infra\n"
            "  features:" + PDM_BLOCK
        )
    for name in nodes:
        chunks.append(
            f"{name}:\n"
            "  config:\n"
            "    type: pve\n"
            f"    hostname: {name}.internal\n"
            "    user: root\n"
            "    sshkey: infra\n"
            "  features:" + NODE_BLOCK
        )
    return "".join(chunks)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def _write(root: Path, *, pdm_hosts: list[str], nodes: list[str]) -> None:
    (root / "hosts.conf").write_text(
        _hosts_conf(pdm_hosts=pdm_hosts, nodes=nodes), encoding="utf-8"
    )


@pytest.fixture
def stub_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every secret to a fixed value; 1Password is not the subject here.

    Both hooks are needed: `validate()` checks the secret exists in the catalog,
    and `deploy()` separately reads its fields. The tmp repo has no secrets/ tree.
    """
    monkeypatch.setattr(
        pve_autoinstall,
        "_read_secret_field",
        lambda root, secret, key: f"{secret}:{key}",
    )
    monkeypatch.setattr(pve_autoinstall, "validate_secret_reference", lambda root, name: None)


class RecordingSession(DeploySession):
    """A DeploySession that records the hosts it was asked to deploy to.

    Subclassed rather than mocked so `deploy()` keeps talking to the real
    run/finish contract, including the failed-host bookkeeping finish() reads.
    """

    def __init__(self) -> None:
        super().__init__("pve-autoinstall")
        self.ran: list[str] = []

    def run(self, deploy_host: Any, hosts: list[str]) -> None:
        self.ran.extend(hosts)


def _deploy(
    root: Path, host: str = "all", *, dry_run: bool = False
) -> tuple[int, RecordingSession]:
    session = RecordingSession()
    code = pve_autoinstall.deploy(root, host, dry_run, False, session)
    return code, session


class TestHostResolution:
    def test_no_pdm_host_is_a_clean_skip(self, repo: Path) -> None:
        """Without a pdm_host there is no server to sync to; that is config, not failure."""
        _write(repo, pdm_hosts=[], nodes=["ace"])

        code, session = _deploy(repo)

        assert code == 0
        assert session.ran == []

    def test_multiple_pdm_hosts_is_a_hard_failure(self, repo: Path, capsys) -> None:
        """Two PDM hosts means answers could be synced to the wrong server. Refuse."""
        _write(repo, pdm_hosts=["arc", "arc2"], nodes=["ace"])

        code, session = _deploy(repo)

        assert code == 1
        assert session.ran == []
        assert "multiple PDM hosts" in capsys.readouterr().err

    def test_requested_host_outside_the_feature_is_a_clean_skip(self, repo: Path) -> None:
        _write(repo, pdm_hosts=["arc"], nodes=["ace"])
        (repo / "hosts.conf").write_text(
            (repo / "hosts.conf").read_text(encoding="utf-8")
            + "helm:\n"
            "  config:\n"
            "    type: lxc\n"
            "    hostname: helm.internal\n"
            "    user: root\n"
            "    sshkey: infra\n"
            "  features: {}\n",
            encoding="utf-8",
        )

        code, session = _deploy(repo, "helm")

        assert code == 0
        assert session.ran == []

    def test_the_pdm_host_is_never_itself_an_install_target(
        self,
        repo: Path,
        stub_secrets: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Targeting the PDM host would build an answer file for the server itself."""
        _write(repo, pdm_hosts=["arc"], nodes=["ace"])

        code, session = _deploy(repo, "arc")

        assert code == 0
        assert session.ran == []


class TestFailurePaths:
    def test_validate_failure_returns_one_without_deploying(
        self,
        repo: Path,
        stub_secrets: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        _write(repo, pdm_hosts=["arc"], nodes=["ace"])
        monkeypatch.setattr(
            pve_autoinstall,
            "validate",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad answer config")),
        )

        code, session = _deploy(repo)

        assert code == 1
        assert session.ran == []
        assert "bad answer config" in capsys.readouterr().err

    def test_missing_pdm_config_key_returns_one(self, repo: Path, capsys) -> None:
        """A PDM host missing a required key must not fall through to a partial sync."""
        conf = _hosts_conf(pdm_hosts=["arc"], nodes=["ace"]).replace(
            "      country: us\n", ""
        )
        (repo / "hosts.conf").write_text(conf, encoding="utf-8")

        code, session = _deploy(repo)

        assert code == 1
        assert session.ran == []
        assert "pve-autoinstall.country missing" in capsys.readouterr().err

    def test_unresolvable_secret_returns_one(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        _write(repo, pdm_hosts=["arc"], nodes=["ace"])
        monkeypatch.setattr(
            pve_autoinstall,
            "validate",
            lambda *args, **kwargs: None,
        )

        def explode(root: Path, secret: str, key: str) -> str:
            raise op_secrets.OpSecretsError(f"{key} is empty in rendered secret '{secret}'")

        monkeypatch.setattr(pve_autoinstall, "_read_secret_field", explode)

        code, session = _deploy(repo)

        assert code == 1
        assert session.ran == []
        assert "is empty in rendered secret" in capsys.readouterr().err

    def test_answer_build_failure_names_the_host_and_stops(
        self,
        repo: Path,
        stub_secrets: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys,
    ) -> None:
        """One unbuildable node aborts the whole sync rather than syncing the rest.

        The plan is pushed to PDM as a set; a partial one would leave the skipped
        node with whatever stale answer PDM already holds.
        """
        _write(repo, pdm_hosts=["arc"], nodes=["ace", "bray"])
        monkeypatch.setattr(pve_autoinstall, "validate", lambda *args, **kwargs: None)

        def explode(root: Path, registry: Any, host: str, cfg: dict) -> dict:
            raise ValueError(f"no management interface for {host}")

        monkeypatch.setattr(pve_autoinstall, "_build_answer_entry", explode)

        code, session = _deploy(repo)

        assert code == 1
        assert session.ran == []
        assert "Failed to build answer for ace" in capsys.readouterr().err


class TestSuccessPaths:
    def test_dry_run_reports_every_answer_and_runs_nothing(
        self,
        repo: Path,
        stub_secrets: None,
        capsys,
    ) -> None:
        _write(repo, pdm_hosts=["arc"], nodes=["ace"])

        code, session = _deploy(repo, dry_run=True)

        assert code == 0
        assert session.ran == []
        output = capsys.readouterr().out
        assert "[DRY-RUN]" in output
        assert "SERIAL123" in output
        assert "10.0.0.11/24" in output

    def test_offline_mode_stops_before_secrets_and_ssh(
        self,
        repo: Path,
        stub_secrets: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(repo, pdm_hosts=["arc"], nodes=["ace"])
        monkeypatch.setenv("HOMELAB_OFFLINE", "1")

        code, session = _deploy(repo)

        assert code == 0
        assert session.ran == []

    def test_live_run_targets_the_pdm_host_only(
        self,
        repo: Path,
        stub_secrets: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sync happens once, on the PDM host — not per PVE node."""
        _write(repo, pdm_hosts=["arc"], nodes=["ace", "bray"])
        monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)

        code, session = _deploy(repo)

        assert code == 0
        assert session.ran == ["arc"]

    def test_live_run_passes_every_node_into_the_plan(
        self,
        repo: Path,
        stub_secrets: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write(repo, pdm_hosts=["arc"], nodes=["ace", "bray"])
        monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
        captured: dict[str, Any] = {}

        def capture(root, registry, pdm_host, plan, token, passwords, force):
            captured["plan"] = plan
            captured["passwords"] = passwords

        monkeypatch.setattr(pve_autoinstall, "_run_on_pdm_host", capture)

        session = RecordingSession()

        # Run the staged callable the way DeploySession would, so the plan actually builds.
        def run(deploy_host, hosts):
            session.ran.extend(hosts)
            for host in hosts:
                deploy_host(host)

        monkeypatch.setattr(session, "run", run)

        code = pve_autoinstall.deploy(repo, "all", False, False, session)

        assert code == 0
        assert [entry["_host"] for entry in captured["plan"]["answers"]] == ["ace", "bray"]
        assert sorted(captured["passwords"]) == ["ace", "bray"]
