"""Direct tests for `homelab.module_support`, the shared module-deploy helpers.

Written against the 2026-09-12 mutation sweep rather than against coverage. That sweep
found this file was the worst-scoring in scope (54.8%), and the reason was structural:
every existing test reached these helpers only through a module's *dry-run*, so the
live-deploy branch of `simple_root_installer_deploy` had no assertions at all, and
`copy_cached_secret` had no test of any kind (17 of 17 mutants "untested"). Coverage
could not see either hole — the dry-run executes the function, it just never checks
what it did.

The assertions here are therefore deliberately exact — argument-for-argument on the
staging call, byte-for-byte on written files, and `0o600` rather than "not world
readable". A looser assertion passes the mutant that matters.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from homelab import module_support, op_secrets
from homelab.deploy import DeploySession

FEATURE = "demo-module"

HOSTS_CONF = """
alpha:
  config:
    type: ubuntu
    hostname: alpha.internal
    user: root
    sshkey: homelab
  features:
    demo-module: {}
beta:
  config:
    type: ubuntu
    hostname: beta.internal
    user: deploy
    sshkey: homelab
  features:
    demo-module: {}
gamma:
  config:
    type: ubuntu
    hostname: gamma.internal
    user: operator
    sshkey: homelab
  features: {}
""".lstrip()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A throwaway repo root: a two-host inventory plus the module's installer.

    Deliberately not the real repo. These tests assert on exact host names, users and
    staged paths, so they must not move every time `hosts.conf` gains a host.
    """
    (tmp_path / "hosts.conf").write_text(HOSTS_CONF, encoding="utf-8")
    installer = tmp_path / FEATURE / "scripts" / "install.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def printed(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Capture the *arguments* to print_action/print_sub, not the rendered output.

    Several mutants replace a formatted message with a bare `None`, which rich prints
    perfectly happily. Recording the argument catches that; scraping stdout would not
    distinguish it from a wrapped line.
    """
    messages: dict[str, list[Any]] = {"action": [], "sub": [], "ok": [], "warn": []}
    for name, key in (
        ("print_action", "action"),
        ("print_sub", "sub"),
        ("print_ok", "ok"),
        ("print_warn", "warn"),
    ):
        monkeypatch.setattr(
            "homelab.output." + name,
            lambda message, _bucket=messages[key]: _bucket.append(message),
        )
    return messages


# --------------------------------------------------------------------------------------
# copy_cached_secret — 17/17 mutants had no test at all before this block.
# --------------------------------------------------------------------------------------


@pytest.fixture
def cached_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand in for the tmpfs secret cache, asserting how it was asked for.

    The stub checks its own arguments so that mutants blanking either one
    (`secret_file(None, name)`, `secret_file(root, None)`) fail here rather than
    silently returning the same file.
    """
    source = tmp_path / "cache" / "pbs-0.env"
    source.parent.mkdir()
    source.write_text("PBS_PASSWORD=from-the-cache\n", encoding="utf-8")

    def fake_secret_file(requested_root: Path, name: str) -> Path:
        assert requested_root == tmp_path
        assert name == "pbs-primary"
        return source

    monkeypatch.setattr(module_support.op_secrets, "secret_file", fake_secret_file)
    return source


def test_copy_cached_secret_copies_the_rendered_bytes(
    tmp_path: Path, cached_secret: Path
) -> None:
    destination = tmp_path / "stage" / "pbs-0.env"

    result = module_support.copy_cached_secret(tmp_path, "pbs-primary", destination)

    assert result == destination
    assert destination.read_text(encoding="utf-8") == "PBS_PASSWORD=from-the-cache\n"


def test_copy_cached_secret_creates_missing_parent_directories(
    tmp_path: Path, cached_secret: Path
) -> None:
    # Two levels deep on purpose: one level would still work with `parents=False`,
    # because tmp_path itself already exists.
    destination = tmp_path / "stage" / "beta" / "pbs-0.env"

    module_support.copy_cached_secret(tmp_path, "pbs-primary", destination)

    assert destination.is_file()


def test_copy_cached_secret_tolerates_an_existing_parent(
    tmp_path: Path, cached_secret: Path
) -> None:
    destination = tmp_path / "stage" / "pbs-0.env"
    destination.parent.mkdir()

    module_support.copy_cached_secret(tmp_path, "pbs-primary", destination)

    assert destination.is_file()


def test_copy_cached_secret_is_owner_only(tmp_path: Path, cached_secret: Path) -> None:
    # The whole point of the staging step: the cache entry's mode is not inherited,
    # and the staged copy is 0600 exactly.
    cached_secret.chmod(0o644)
    destination = tmp_path / "stage" / "pbs-0.env"

    module_support.copy_cached_secret(tmp_path, "pbs-primary", destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_copy_cached_secret_overwrites_and_re_secures_an_existing_file(
    tmp_path: Path, cached_secret: Path
) -> None:
    """Restaging over a stale, world-readable file must replace it, not merge with it.

    Also pins the copy direction: with the arguments swapped this would leave the old
    contents in place and quietly rewrite the *cache* instead.
    """
    destination = tmp_path / "stage" / "pbs-0.env"
    destination.parent.mkdir()
    destination.write_text("PBS_PASSWORD=stale\n", encoding="utf-8")
    destination.chmod(0o644)

    module_support.copy_cached_secret(tmp_path, "pbs-primary", destination)

    assert destination.read_text(encoding="utf-8") == "PBS_PASSWORD=from-the-cache\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert cached_secret.read_text(encoding="utf-8") == "PBS_PASSWORD=from-the-cache\n"


def test_copy_cached_secret_propagates_an_unresolvable_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret that will not render must fail the deploy, not stage an empty file."""

    def explode(_root: Path, name: str) -> Path:
        raise op_secrets.OpSecretsError(f"unknown secret '{name}'")

    monkeypatch.setattr(module_support.op_secrets, "secret_file", explode)
    destination = tmp_path / "stage" / "pbs-0.env"

    with pytest.raises(op_secrets.OpSecretsError):
        module_support.copy_cached_secret(tmp_path, "pbs-primary", destination)

    assert not destination.exists()


# --------------------------------------------------------------------------------------
# connection_for_host
# --------------------------------------------------------------------------------------


def test_connection_for_host_reads_user_and_hostname_from_the_inventory(
    root: Path,
) -> None:
    connection = module_support.connection_for_host(root, "beta")

    assert connection.host == "beta"
    assert connection.connection.user == "deploy"
    assert connection.connection.host == "beta.internal"


def test_connection_for_host_reads_each_host_separately(root: Path) -> None:
    """Two hosts, two different users: the lookup is per-host, not cached across them."""
    alpha = module_support.connection_for_host(root, "alpha")
    gamma = module_support.connection_for_host(root, "gamma")

    assert (alpha.connection.host, alpha.connection.user) == ("alpha.internal", "root")
    assert (gamma.connection.host, gamma.connection.user) == ("gamma.internal", "operator")


def test_connection_for_host_rejects_an_unknown_host(root: Path) -> None:
    from homelab.hosts import HostLookupError

    with pytest.raises(HostLookupError):
        module_support.connection_for_host(root, "nowhere")


# --------------------------------------------------------------------------------------
# simple_root_installer_deploy
# --------------------------------------------------------------------------------------


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every `stage_and_run_remote_installer` call instead of making it.

    Patched on `homelab.deploy` because `simple_root_installer_deploy` imports the
    symbol inside the function body, at call time.
    """
    calls: list[dict[str, Any]] = []

    def record(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr("homelab.deploy.stage_and_run_remote_installer", record)
    return calls


def test_simple_root_installer_deploy_stages_the_expected_bundle(
    root: Path, staged: list[dict[str, Any]]
) -> None:
    """Pin the staging call argument for argument.

    This is the assertion the dry-run smoke test structurally cannot make: it never
    reaches this branch, so `require_root`, the installer path and the uploaded
    directory pair were all unasserted. `require_root=False` in particular would
    deploy every root-owned module as an unprivileged user and still exit 0.
    """
    session = DeploySession(FEATURE)

    exit_code = module_support.simple_root_installer_deploy(
        root,
        "beta",
        False,
        False,
        session,
        feature=FEATURE,
        remote_root="/tmp/homelab-demo",
        env_for_host=lambda host: {"TARGET": host},
    )

    assert exit_code == 0
    assert len(staged) == 1
    args = staged[0]["args"]
    kwargs = staged[0]["kwargs"]

    assert args[0] == root
    assert args[1].host == "beta"
    assert args[2] == "/tmp/homelab-demo"
    assert args[3] == [(root / FEATURE / "scripts", "/tmp/homelab-demo/scripts")]
    assert args[4] == "scripts/install.sh"
    assert args[5] == "beta"
    assert kwargs == {
        "env": {"TARGET": "beta"},
        "require_root": True,
        "remote_subdirs": ("lib",),
    }


def test_simple_root_installer_deploy_without_env_passes_none(
    root: Path, staged: list[dict[str, Any]]
) -> None:
    module_support.simple_root_installer_deploy(
        root,
        "beta",
        False,
        False,
        DeploySession(FEATURE),
        feature=FEATURE,
        remote_root="/tmp/homelab-demo",
    )

    assert staged[0]["kwargs"]["env"] is None


def test_simple_root_installer_deploy_targets_every_enabled_host(
    root: Path, staged: list[dict[str, Any]]
) -> None:
    module_support.simple_root_installer_deploy(
        root,
        "all",
        False,
        False,
        DeploySession(FEATURE),
        feature=FEATURE,
        remote_root="/tmp/homelab-demo",
    )

    assert [call["args"][5] for call in staged] == ["alpha", "beta"]


def test_simple_root_installer_deploy_skips_a_host_without_the_feature(
    root: Path, staged: list[dict[str, Any]]
) -> None:
    exit_code = module_support.simple_root_installer_deploy(
        root,
        "gamma",
        False,
        False,
        DeploySession(FEATURE),
        feature=FEATURE,
        remote_root="/tmp/homelab-demo",
    )

    assert exit_code == 0
    assert staged == []


def test_simple_root_installer_deploy_dry_run_stages_nothing(
    root: Path, staged: list[dict[str, Any]], printed: dict[str, list[Any]]
) -> None:
    exit_code = module_support.simple_root_installer_deploy(
        root,
        "beta",
        True,
        False,
        DeploySession(FEATURE),
        feature=FEATURE,
        remote_root="/tmp/homelab-demo",
        dry_run_details=lambda host: [f"would restart {host}"],
    )

    assert exit_code == 0
    assert staged == []
    assert f"[DRY-RUN] Would deploy {FEATURE} to beta" in printed["action"]
    assert printed["sub"] == ["would restart beta"]


def test_simple_root_installer_deploy_dry_run_details_are_optional(
    root: Path, printed: dict[str, list[Any]]
) -> None:
    module_support.simple_root_installer_deploy(
        root,
        "beta",
        True,
        False,
        DeploySession(FEATURE),
        feature=FEATURE,
        remote_root="/tmp/homelab-demo",
    )

    # `printed` only sees module_support's own late-bound imports; DeploySession binds
    # print_sub at import time, so its "Hosts: ..." banner is not in this bucket.
    assert printed["sub"] == []


def test_simple_root_installer_deploy_rejects_a_missing_installer(root: Path) -> None:
    """Pre-flight, not per-host: a missing installer must fail before any host runs.

    Dropping the `validate=` argument entirely still exits 0 on a dry run, so the
    error message is asserted rather than just the exception type.
    """
    (root / FEATURE / "scripts" / "install.sh").unlink()

    with pytest.raises(ValueError, match=r"Missing installer: .*install\.sh"):
        module_support.simple_root_installer_deploy(
            root,
            "all",
            True,
            False,
            DeploySession(FEATURE),
            feature=FEATURE,
            remote_root="/tmp/homelab-demo",
        )


def test_simple_root_installer_deploy_reports_a_failed_host(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that raises is collected, not fatal, and turns the exit code non-zero."""

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("ssh died")

    monkeypatch.setattr("homelab.deploy.stage_and_run_remote_installer", explode)
    session = DeploySession(FEATURE)

    exit_code = module_support.simple_root_installer_deploy(
        root,
        "all",
        False,
        False,
        session,
        feature=FEATURE,
        remote_root="/tmp/homelab-demo",
    )

    assert exit_code == 1
    assert session.failed_hosts == ["alpha", "beta"]


# --------------------------------------------------------------------------------------
# run_module_deploy
# --------------------------------------------------------------------------------------


def test_run_module_deploy_passes_both_host_lists_to_validate(root: Path) -> None:
    """`validate` receives (supported, filtered) — callers rely on the distinction.

    pve-backup validates across every configured host even when deploying to one, so
    collapsing these two arguments into the same list is a real behaviour change.
    """
    seen: list[tuple[list[str], list[str]]] = []

    exit_code = module_support.run_module_deploy(
        root,
        "beta",
        FEATURE,
        DeploySession(FEATURE),
        lambda _host: None,
        validate=lambda supported, hosts: seen.append((supported, hosts)),
    )

    assert exit_code == 0
    assert seen == [(["alpha", "beta"], ["beta"])]


def test_run_module_deploy_skips_before_validating(root: Path) -> None:
    """The applicability skip short-circuits validation; it must not run at all."""

    def fail(_supported: list[str], _hosts: list[str]) -> None:
        raise AssertionError("validate ran for a host the feature is not enabled on")

    assert (
        module_support.run_module_deploy(
            root, "gamma", FEATURE, DeploySession(FEATURE), lambda _host: None, validate=fail
        )
        == 0
    )


def test_run_module_deploy_skip_names_the_feature_and_target(
    root: Path, printed: dict[str, list[Any]]
) -> None:
    module_support.run_module_deploy(
        root, "gamma", FEATURE, DeploySession(FEATURE), lambda _host: None
    )

    assert printed["action"] == [f"Skipping {FEATURE} (not applicable to gamma)"]


def test_run_module_deploy_returns_one_when_a_host_fails(root: Path) -> None:
    def explode(host: str) -> None:
        if host == "beta":
            raise RuntimeError("nope")

    session = DeploySession(FEATURE)

    assert module_support.run_module_deploy(root, "all", FEATURE, session, explode) == 1
    assert session.failed_hosts == ["beta"]


def test_run_module_deploy_rejects_an_unknown_host(root: Path) -> None:
    from homelab.hosts import HostLookupError

    with pytest.raises(HostLookupError):
        module_support.run_module_deploy(
            root, "nowhere", FEATURE, DeploySession(FEATURE), lambda _host: None
        )


# --------------------------------------------------------------------------------------
# stage_encryption_keyfile
#
# Moved here from test_pbs_client_backup.py: the helper is in module_support and both
# pbs-client-backup and pve-backup write the same /etc/homelab/pbs-encryption.key, so
# its tests belong with the helper rather than with one of its two callers.
#
# The parsing here is picky on purpose. `proxmox-backup-client` accepts the file as
# `--keyfile` with no schema check, so a keyfile that is subtly wrong — an extra
# trailing quote, a truncated object — produces archives that restore to nothing. The
# mutation sweep found the validation was one `or` away from accepting exactly that.
# --------------------------------------------------------------------------------------

KEYFILE_JSON = (
    '{"kdf":null,"created":"2026-01-01T00:00:00+00:00",'
    '"modified":"2026-01-01T00:00:00+00:00","data":"AAA=",'
    '"fingerprint":"aa:bb"}'
)


@pytest.fixture
def rendered_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a fake rendered secret env and return a setter for its key line."""
    rendered = tmp_path / "rendered.env"

    def set_key(value: str) -> Path:
        rendered.write_text(
            f"PBS_ENCRYPTION_KEY={value}\nPBS_ENCRYPTION_FINGERPRINT=aa:bb\n",
            encoding="utf-8",
        )
        return rendered

    def fake_secret_file(requested_root: Path, name: str) -> Path:
        # Asserted, not ignored: a mutant that blanks either argument must fail here.
        assert requested_root == tmp_path
        assert name == module_support.ENCRYPTION_KEY_SECRET
        return rendered

    monkeypatch.setattr(module_support.op_secrets, "secret_file", fake_secret_file)
    return set_key


def test_stage_encryption_keyfile_writes_raw_json(tmp_path: Path, rendered_secret) -> None:
    rendered_secret(KEYFILE_JSON)
    dest = tmp_path / "out" / "pbs-encryption.key"

    assert module_support.stage_encryption_keyfile(tmp_path, dest) == dest
    assert dest.read_text(encoding="utf-8") == KEYFILE_JSON + "\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


@pytest.mark.parametrize("quote", ['"', "'"])
def test_stage_encryption_keyfile_strips_matched_quotes(
    tmp_path: Path, rendered_secret, quote: str
) -> None:
    """Both quoting styles are stripped, and stripped by exactly one character."""
    rendered_secret(f"{quote}{KEYFILE_JSON}{quote}")
    dest = tmp_path / "out" / "pbs-encryption.key"

    module_support.stage_encryption_keyfile(tmp_path, dest)

    assert dest.read_text(encoding="utf-8") == KEYFILE_JSON + "\n"


@pytest.mark.parametrize("quote", ['"', "'"])
def test_stage_encryption_keyfile_rejects_an_unbalanced_quote(
    tmp_path: Path, rendered_secret, quote: str
) -> None:
    """A leading quote with no closing one is malformed, not something to strip.

    This is the case the surviving `and` -> `or` mutant accepted: stripping on either
    end alone turns `"{...}` into a truncated object that then passes the `{` check
    and gets written as a keyfile.
    """
    rendered_secret(f"{quote}{KEYFILE_JSON}")

    with pytest.raises(op_secrets.OpSecretsError):
        module_support.stage_encryption_keyfile(tmp_path, tmp_path / "out.key")


def test_stage_encryption_keyfile_rejects_non_json(tmp_path: Path, rendered_secret) -> None:
    rendered_secret("not-json")

    with pytest.raises(op_secrets.OpSecretsError, match="did not render a PBS keyfile"):
        module_support.stage_encryption_keyfile(tmp_path, tmp_path / "out.key")


def test_stage_encryption_keyfile_rejects_json_without_a_fingerprint(
    tmp_path: Path, rendered_secret
) -> None:
    """A JSON object is not enough — both halves of the check must hold.

    `proxmox-backup-client` identifies a key by its fingerprint; an object without one
    is not a keyfile, and the `or` -> `and` mutant here let it through.
    """
    rendered_secret('{"kdf":null,"data":"AAA="}')

    with pytest.raises(op_secrets.OpSecretsError, match="did not render a PBS keyfile"):
        module_support.stage_encryption_keyfile(tmp_path, tmp_path / "out.key")


def test_stage_encryption_keyfile_rejects_a_secret_without_the_key_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PBS_ENCRYPTION_KEY= line at all fails the same way a malformed one does."""
    rendered = tmp_path / "rendered.env"
    rendered.write_text("PBS_ENCRYPTION_FINGERPRINT=aa:bb\n", encoding="utf-8")
    monkeypatch.setattr(
        module_support.op_secrets, "secret_file", lambda _root, _name: rendered
    )

    with pytest.raises(op_secrets.OpSecretsError, match="did not render a PBS keyfile"):
        module_support.stage_encryption_keyfile(tmp_path, tmp_path / "out.key")


def test_stage_encryption_keyfile_takes_the_first_key_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second PBS_ENCRYPTION_KEY line must not silently win over the first."""
    other = (
        '{"kdf":null,"created":"2026-02-02T00:00:00+00:00",'
        '"data":"BBB=","fingerprint":"cc:dd"}'
    )
    rendered = tmp_path / "rendered.env"
    rendered.write_text(
        f"PBS_ENCRYPTION_KEY={KEYFILE_JSON}\nPBS_ENCRYPTION_KEY={other}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module_support.op_secrets, "secret_file", lambda _root, _name: rendered
    )
    dest = tmp_path / "out" / "pbs-encryption.key"

    module_support.stage_encryption_keyfile(tmp_path, dest)

    assert dest.read_text(encoding="utf-8") == KEYFILE_JSON + "\n"


def test_stage_encryption_keyfile_creates_missing_parent_directories(
    tmp_path: Path, rendered_secret
) -> None:
    # Two levels: one level alone would still work without `parents=True`.
    rendered_secret(KEYFILE_JSON)
    dest = tmp_path / "stage" / "ace" / "pbs-encryption.key"

    module_support.stage_encryption_keyfile(tmp_path, dest)

    assert dest.is_file()


def test_stage_encryption_keyfile_tolerates_an_existing_parent(
    tmp_path: Path, rendered_secret
) -> None:
    rendered_secret(KEYFILE_JSON)
    dest = tmp_path / "stage" / "pbs-encryption.key"
    dest.parent.mkdir()

    module_support.stage_encryption_keyfile(tmp_path, dest)

    assert dest.is_file()
