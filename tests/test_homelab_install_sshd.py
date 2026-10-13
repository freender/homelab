"""`homelab_install.sshd` and `homelab_install.sudoers` (freender/homelab-ops#162).

Shared by `ubuntu-setup` and `guest-baseline`. Every command is faked; nothing
here reads a real sshd or sudoers. Assertions are exact on the two silent
failure modes: a setting the check never compares (so an override goes
unnoticed) and a reload that never happens (so a written drop-in never applies).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from homelab_install import files, sshd, sudoers
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

DROP_IN = (
    "# Managed by homelab\n"
    "PermitRootLogin prohibit-password\n"
    "PasswordAuthentication no\n"
    "ChallengeResponseAuthentication no\n"
    "X11Forwarding no\n"
    "MaxAuthTries 3\n"
)
EFFECTIVE = (
    "port 22\n"
    "permitrootlogin without-password\n"
    "passwordauthentication no\n"
    "kbdinteractiveauthentication no\n"
    "x11forwarding no\n"
    "maxauthtries 3\n"
)


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.answers: dict[tuple[str, ...], tuple[int, str]] = {}

    def answer(self, *argv: str, code: int = 0, stdout: str = "") -> None:
        self.answers[argv] = (code, stdout)

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        code, stdout = self.answers.get(tuple(argv), (0, ""))
        return subprocess.CompletedProcess(argv, code, stdout=stdout)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    run = FakeRun()
    run.answer("sshd", "-T", stdout=EFFECTIVE)
    run.answer("systemctl", "is-active", "--quiet", "ssh.socket", code=3)
    for target in (sshd, sudoers, files):
        monkeypatch.setattr(target, "_run", run)
    return run


def write(tmp_path: Path, text: str = DROP_IN) -> Path:
    path = tmp_path / "drop-in.conf"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# expected_settings / effective_settings
# ---------------------------------------------------------------------------


def test_expected_settings_are_keyed_and_valued_as_sshd_t_prints_them(tmp_path: Path) -> None:
    assert sshd.expected_settings(write(tmp_path)) == {
        "permitrootlogin": "without-password",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "x11forwarding": "no",
        "maxauthtries": "3",
    }


def test_expected_settings_skip_comments_blank_and_bare_lines_and_join_values(
    tmp_path: Path,
) -> None:
    text = "#PasswordAuthentication yes\n\nUsePAM\n  AcceptEnv LANG  LC_*\n"

    assert sshd.expected_settings(write(tmp_path, text)) == {"acceptenv": "lang lc_*"}


def test_effective_settings_parse_sshd_t(fake: FakeRun) -> None:
    fake.answer("sshd", "-T", stdout="Port 22\nAcceptEnv LANG LC_*\nbare\n")

    assert sshd.effective_settings() == {"port": "22", "acceptenv": "lang lc_*", "bare": ""}
    assert fake.calls == [["sshd", "-T"]]
    assert fake.kwargs[0] == {"check": False, "capture_output": True, "text": True}


def test_no_sshd_t_output_is_no_settings(fake: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sshd, "_run", lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, stdout=None)
    )

    assert sshd.effective_settings() == {}


def test_a_failing_sshd_t_raises(fake: FakeRun) -> None:
    fake.answer("sshd", "-T", code=255)

    with pytest.raises(InstallError, match=r"^sshd -T failed \(exit 255\)$"):
        sshd.effective_settings()


# ---------------------------------------------------------------------------
# effective_differences / verify_effective
# ---------------------------------------------------------------------------


def test_a_matching_config_has_no_differences(tmp_path: Path, fake: FakeRun) -> None:
    assert sshd.effective_differences(write(tmp_path)) == []


def test_every_differing_and_missing_setting_is_reported(tmp_path: Path, fake: FakeRun) -> None:
    fake.answer(
        "sshd",
        "-T",
        stdout=EFFECTIVE.replace("x11forwarding no", "x11forwarding yes").replace(
            "maxauthtries 3\n", ""
        ),
    )

    assert sshd.effective_differences(write(tmp_path)) == [
        "x11forwarding is yes, expected no",
        "maxauthtries is (unset), expected 3",
    ]


def test_verify_effective_names_the_drop_in_and_every_difference(
    tmp_path: Path, fake: FakeRun
) -> None:
    fake.answer(
        "sshd",
        "-T",
        stdout=EFFECTIVE.replace("passwordauthentication no", "x y").replace(
            "maxauthtries 3", "maxauthtries 6"
        ),
    )

    with pytest.raises(InstallError) as raised:
        sshd.verify_effective(write(tmp_path), "/etc/ssh/sshd_config.d/99-x.conf")

    assert str(raised.value) == (
        "effective sshd config overrides /etc/ssh/sshd_config.d/99-x.conf: "
        "passwordauthentication is (unset), expected no; maxauthtries is 6, expected 3. "
        "sshd keeps the first value it reads, so an earlier file in /etc/ssh/sshd_config.d "
        "or sshd_config wins"
    )


def test_verify_effective_passes_quietly_on_a_match(
    tmp_path: Path, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    sshd.verify_effective(write(tmp_path), "/d")

    assert capsys.readouterr().out == (
        "    \u2713 Effective sshd config matches the hardening drop-in\n"
    )


# ---------------------------------------------------------------------------
# reload / harden
# ---------------------------------------------------------------------------


IS_SOCKET_ACTIVE = ["systemctl", "is-active", "--quiet", "ssh.socket"]


def test_reload_reloads_ssh_and_raises_on_failure(
    fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    sshd.reload()
    assert capsys.readouterr().out == "    \u2713 SSH reloaded\n"
    assert fake.calls == [IS_SOCKET_ACTIVE, ["systemctl", "reload", "ssh"]]
    assert fake.kwargs == [{"check": False}, {"check": False, "stdout": subprocess.DEVNULL}]

    fake.answer("systemctl", "reload", "ssh", code=1)
    with pytest.raises(InstallError, match=r"^systemctl reload ssh failed \(exit 1\)$"):
        sshd.reload()


def test_a_socket_activated_sshd_is_restarted_not_reloaded(
    fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    """A SIGHUP'd sshd tries to bind port 22 itself, which ssh.socket holds."""
    fake.answer(*IS_SOCKET_ACTIVE, code=0)

    sshd.reload()

    assert capsys.readouterr().out == "    \u2713 SSH restarted (socket-activated)\n"
    assert fake.calls == [IS_SOCKET_ACTIVE, ["systemctl", "restart", "ssh.service"]]

    fake.answer("systemctl", "restart", "ssh.service", code=1)
    with pytest.raises(InstallError, match=r"^systemctl restart ssh.service failed \(exit 1\)$"):
        sshd.reload()


class Hardened:
    def __init__(self, tmp_path: Path) -> None:
        self.dest = tmp_path / "etc" / "99-homelab.conf"
        self.retired = tmp_path / "etc" / "99-old.conf"
        build = tmp_path / "build"
        build.mkdir()
        (build / "drop-in").write_text(DROP_IN, encoding="utf-8")
        self.ctx = InstallContext(
            host="h",
            script_dir=tmp_path,
            build_dir=build,
            env={},
            deploy_env={},
            file_map={"drop-in": (str(self.dest), "644")},
            force_update=False,
        )
        self.dest.parent.mkdir()

    def harden(self) -> None:
        sshd.harden(self.ctx, "drop-in", (str(self.retired),))


@pytest.fixture
def hardened(tmp_path: Path) -> Hardened:
    return Hardened(tmp_path)


def test_a_converged_drop_in_is_only_verified(hardened: Hardened, fake: FakeRun) -> None:
    hardened.dest.write_text(DROP_IN, encoding="utf-8")

    hardened.harden()

    assert fake.calls == [["sshd", "-T"]]


def test_a_new_drop_in_is_validated_reloaded_then_verified(
    hardened: Hardened, fake: FakeRun
) -> None:
    hardened.harden()

    assert hardened.dest.read_text(encoding="utf-8") == DROP_IN
    assert fake.calls == [
        ["sshd", "-t"],
        IS_SOCKET_ACTIVE,
        ["systemctl", "reload", "ssh"],
        ["sshd", "-T"],
    ]


def test_removing_only_a_retired_drop_in_still_reloads(
    hardened: Hardened, fake: FakeRun, capsys: pytest.CaptureFixture[str]
) -> None:
    hardened.dest.write_text(DROP_IN, encoding="utf-8")
    hardened.retired.write_text("PasswordAuthentication no\n", encoding="utf-8")

    hardened.harden()

    assert not hardened.retired.exists()
    assert f"    Removed {hardened.retired} (superseded)\n" in capsys.readouterr().out
    assert fake.calls == [IS_SOCKET_ACTIVE, ["systemctl", "reload", "ssh"], ["sshd", "-T"]]


def test_a_rejected_drop_in_keeps_the_retired_one_and_never_reloads(
    hardened: Hardened, fake: FakeRun
) -> None:
    hardened.retired.write_text("PasswordAuthentication no\n", encoding="utf-8")
    fake.answer("sshd", "-t", code=255)

    with pytest.raises(InstallError, match="rolled back"):
        hardened.harden()

    assert hardened.retired.exists()
    assert not hardened.dest.exists()
    assert fake.calls == [["sshd", "-t"]]


def test_harden_verifies_against_the_installed_destination(
    hardened: Hardened, fake: FakeRun
) -> None:
    hardened.dest.write_text(DROP_IN, encoding="utf-8")
    fake.answer("sshd", "-T", stdout="")

    with pytest.raises(InstallError, match=f"overrides {hardened.dest}: "):
        hardened.harden()


# ---------------------------------------------------------------------------
# sudoers
# ---------------------------------------------------------------------------


def test_a_valid_staged_sudoers_file_passes(tmp_path: Path, fake: FakeRun) -> None:
    sudoers.check_staged(tmp_path / "sudoers")

    assert fake.calls == [["visudo", "-cf", str(tmp_path / "sudoers")]]
    assert fake.kwargs[0] == {"check": False, "stdout": subprocess.DEVNULL}


def test_an_invalid_staged_sudoers_file_is_refused(tmp_path: Path, fake: FakeRun) -> None:
    fake.answer("visudo", "-cf", str(tmp_path / "sudoers"), code=1)

    with pytest.raises(InstallError, match="^staged sudoers file is invalid; refusing to install$"):
        sudoers.check_staged(tmp_path / "sudoers")
