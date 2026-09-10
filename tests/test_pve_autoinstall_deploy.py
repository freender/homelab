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

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from invoke.exceptions import UnexpectedExit
from invoke.runners import Result

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


# ---------------------------------------------------------------------------
# _run_on_pdm_host: the staging step deploy() delegates to.
#
# Every test above stubs this out, which left the one function that writes
# plaintext PVE root passwords to disk unexercised. What matters here is the
# file map and the cleanup: the token file must be 0600, the remote staging dir
# must be torn down even when the sync run fails, and the local tmpfs stage must
# be released on both paths. A leaked staging dir is a root password at rest.
# ---------------------------------------------------------------------------


class _StubConnection:
    def __init__(self, host: str, user: str | None = None, hostname: str | None = None) -> None:
        self.host = host
        self.user = user
        self.hostname = hostname
        self.connection = self


class _StubRegistry:
    def get(self, host: str, key: str) -> str:
        return {"config.user": "root", "config.hostname": f"{host}.internal"}[key]


@pytest.fixture
def pdm_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Stub the connection, the tmpfs stage, and the remote installer.

    The staged files are snapshotted at the moment the installer would run,
    because the stage is torn down before _run_on_pdm_host returns.
    """
    state: dict[str, Any] = {
        "stage_prefix": None,
        "stages": [],
        "stage_dir": None,
        "stage_open": False,
        "installer": None,
        "cleanups": [],
        "files": {},
        "modes": {},
        "raise_from_installer": None,
    }

    @contextlib.contextmanager
    def fake_stage(prefix: str):
        state["stage_prefix"] = prefix
        stage_dir = tmp_path / f"stage-{len(state['stages'])}"
        stage_dir.mkdir()
        state["stages"].append(stage_dir)
        state["stage_dir"] = stage_dir
        state["stage_open"] = True
        try:
            yield stage_dir
        finally:
            state["stage_open"] = False

    def fake_installer(root, connection, remote_root, upload_paths, installer, *args, **kwargs):
        state["installer"] = {
            "root": root,
            "connection": connection,
            "remote_root": remote_root,
            "upload_paths": upload_paths,
            "installer": installer,
            "args": args,
            **kwargs,
        }
        for local_path, _remote in upload_paths:
            if local_path.is_file():
                state["files"][local_path.name] = local_path.read_text(encoding="utf-8")
                state["modes"][local_path.name] = local_path.stat().st_mode & 0o777
        if state["raise_from_installer"] is not None:
            raise state["raise_from_installer"]

    monkeypatch.setattr(pve_autoinstall, "HostConnection", _StubConnection)
    monkeypatch.setattr(pve_autoinstall, "tmpfs_secret_stage", fake_stage)
    monkeypatch.setattr(pve_autoinstall, "stage_and_run_remote_installer", fake_installer)
    monkeypatch.setattr(
        pve_autoinstall,
        "_cleanup_remote_pdm_dir",
        lambda connection: state["cleanups"].append(connection),
    )
    return state


def _run_pdm(root: Path, force: bool = False, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "plan": {"answers": [{"_host": "ace"}]},
        "pdm_token_secret": "pdm-token-value",
        "root_passwords": {"bray": "bray-pw", "ace": "ace-pw"},
    }
    kwargs.update(overrides)
    pve_autoinstall._run_on_pdm_host(
        root,
        _StubRegistry(),
        "arc",
        kwargs["plan"],
        kwargs["pdm_token_secret"],
        kwargs["root_passwords"],
        force,
    )


class TestRunOnPdmHost:
    def test_stages_the_plan_token_and_script_to_the_remote_root(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        _run_pdm(tmp_path)

        remote = [target for _local, target in pdm_run["installer"]["upload_paths"]]
        assert remote == [
            f"{pve_autoinstall.REMOTE_ROOT}/answer-plan.json",
            f"{pve_autoinstall.REMOTE_ROOT}/pdm-api-token",
            f"{pve_autoinstall.REMOTE_ROOT}/sync-answers.py",
        ]
        assert pdm_run["installer"]["upload_paths"][2][0] == (
            tmp_path / "pve-autoinstall" / "scripts" / "sync-answers.py"
        )

    def test_plan_is_written_as_json(self, tmp_path: Path, pdm_run: dict[str, Any]) -> None:
        _run_pdm(tmp_path, plan={"answers": [{"_host": "ace", "fqdn": "ace.internal"}]})

        assert json.loads(pdm_run["files"]["answer-plan.json"]) == {
            "answers": [{"_host": "ace", "fqdn": "ace.internal"}]
        }

    def test_token_file_carries_the_pdm_token_then_sorted_root_passwords(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        _run_pdm(tmp_path)

        assert pdm_run["files"]["pdm-api-token"].splitlines() == [
            "PDM_DEPLOY_TOKEN=pdm-token-value",
            "PVE_ROOT_PASSWORD__ace=ace-pw",
            "PVE_ROOT_PASSWORD__bray=bray-pw",
        ]

    def test_token_file_is_not_readable_by_group_or_world(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        _run_pdm(tmp_path)

        assert pdm_run["modes"]["pdm-api-token"] == 0o600

    def test_secrets_are_staged_in_tmpfs_and_released_before_returning(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        _run_pdm(tmp_path)

        assert pdm_run["stage_prefix"] == "homelab-pve-autoinstall."
        assert pdm_run["stage_open"] is False

    def test_runs_the_sync_script_under_python3_with_only_a_lib_subdir(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        _run_pdm(tmp_path)

        call = pdm_run["installer"]
        assert call["installer"] == "sync-answers.py"
        assert call["interpreter"] == "python3"
        assert call["remote_subdirs"] == ("lib",)

    def test_force_flag_is_only_passed_when_requested(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        _run_pdm(tmp_path, force=False)
        assert pdm_run["installer"]["args"] == ()

        _run_pdm(tmp_path, force=True)
        assert pdm_run["installer"]["args"] == ("--force",)

    def test_connects_to_the_pdm_host_using_its_inventory_credentials(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        _run_pdm(tmp_path)

        connection = pdm_run["installer"]["connection"]
        assert (connection.host, connection.user, connection.hostname) == (
            "arc",
            "root",
            "arc.internal",
        )

    def test_remote_staging_dir_is_cleaned_up_on_success(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        _run_pdm(tmp_path)

        assert len(pdm_run["cleanups"]) == 1

    def test_remote_staging_dir_is_cleaned_up_when_the_sync_run_fails(
        self, tmp_path: Path, pdm_run: dict[str, Any]
    ) -> None:
        """The failing dir is the one still holding the root passwords."""
        pdm_run["raise_from_installer"] = RuntimeError("sync-answers.py exited 1")

        with pytest.raises(RuntimeError):
            _run_pdm(tmp_path)

        assert len(pdm_run["cleanups"]) == 1
        assert pdm_run["stage_open"] is False  # local tmpfs stage released too


class TestCleanupRemotePdmDir:
    def test_removes_the_remote_staging_dir(self) -> None:
        commands: list[tuple[str, dict]] = []

        class Connection:
            def run(self, command: str, **kwargs) -> None:
                commands.append((command, kwargs))

        class Wrapper:
            connection = Connection()

        pve_autoinstall._cleanup_remote_pdm_dir(Wrapper())

        assert commands[0][0] == f'rm -rf "{pve_autoinstall.REMOTE_ROOT}"'
        assert commands[0][1] == {"hide": True, "warn": True}

    @pytest.mark.parametrize(
        "error",
        [
            OSError("connection reset"),
            UnexpectedExit(result=Result(command="rm -rf ...", exited=1)),
        ],
    )
    def test_a_failed_cleanup_warns_instead_of_failing_the_deploy(
        self, capsys, error: Exception
    ) -> None:
        """sync-answers.py has already run by this point; the deploy succeeded."""

        class Connection:
            def run(self, command: str, **kwargs) -> None:
                raise error

        class Wrapper:
            connection = Connection()

        pve_autoinstall._cleanup_remote_pdm_dir(Wrapper())  # must not raise

        assert "could not confirm remote cleanup" in capsys.readouterr().out
