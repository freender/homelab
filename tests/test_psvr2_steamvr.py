"""solar's headset-off path: `psvr2-steamvr stop` and `quit`, run against stub commands.

The stubs keep the "running" SteamVR processes in a file; each test picks which
request finally kills them: vrmonitor's quit, SIGTERM, SIGKILL, or nothing.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "vr-guest/files/home/.local/bin/psvr2-steamvr"
STEAMVR = ("vrmonitor", "vrdashboard", "vrwebhelper", "vrcompositor", "vrserver")

STUBS = {
    "pgrep": r"""
pat= list=
while (( $# )); do
    case "$1" in -x) pat=$2; shift ;; -l) list=1 ;; -u) shift ;; esac
    shift
done
rc=1
while read -r p; do
    [[ -n "$p" && "$p" =~ ^($pat)$ ]] || continue
    rc=0
    [[ -n "$list" ]] && echo "1 $p"
done < "$FAKE/procs"
exit "$rc"
""",
    "pkill": r"""
sig=
for a in "$@"; do case "$a" in -TERM|-KILL) sig=$a ;; esac; done
echo "pkill $sig" >> "$FAKE/log"
[[ "$(cat "$FAKE/dies_on")" == "${sig#-}" ]] && : > "$FAKE/procs"
exit 0
""",
    "systemctl": r"""
case "$*" in
    *is-active*) [[ -e "$FAKE/headset" ]] ;;
    *is-system-running*) cat "$FAKE/system_state" 2>/dev/null || echo running ;;
esac
""",
    "ps": r"""
case "$*" in
    *args=*) cat "$FAKE/ps_helper" 2>/dev/null ;;
    *) cat "$FAKE/ps_client" 2>/dev/null || exit 1 ;;
esac
""",
    "sleep": 'echo "sleep $*" >> "$FAKE/log"',
    "flock": "exit 0",
    "timeout": 'shift; exec "$@"',
    "kscreen-doctor": 'echo "kscreen-doctor $*" >> "$FAKE/log"',
    "systemd-run": 'echo "systemd-run $*" >> "$FAKE/log"',
    "steam": 'echo "steam $*" >> "$FAKE/log"',
}

VRMONITOR = r"""#!/bin/bash
echo "vrmonitor.sh $*" >> "$FAKE/log"
[[ "$(cat "$FAKE/dies_on")" == quit ]] && : > "$FAKE/procs"
exit 0
"""


@dataclass
class Solar:
    fake: Path
    runtime: Path
    env: dict[str, str]

    def procs(self, *names: str) -> None:
        (self.fake / "procs").write_text("".join(f"{n}\n" for n in names), encoding="utf-8")

    def dies_on(self, what: str) -> None:
        (self.fake / "dies_on").write_text(what, encoding="utf-8")

    def headset(self, present: bool) -> None:
        path = self.fake / "headset"
        path.touch() if present else path.unlink(missing_ok=True)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(SCRIPT), *args], env=self.env, capture_output=True,
                              text=True, check=False, timeout=30)

    @property
    def log(self) -> list[str]:
        path = self.fake / "log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.fixture
def solar(tmp_path: Path) -> Solar:
    fake, bin_dir, home, runtime = (tmp_path / d for d in ("fake", "bin", "home", "run"))
    for directory in (fake, bin_dir, runtime):
        directory.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    vrmonitor = home / ".local/share/Steam/steamapps/common/SteamVR/bin/vrmonitor.sh"
    vrmonitor.parent.mkdir(parents=True)
    vrmonitor.write_text(VRMONITOR, encoding="utf-8")
    vrmonitor.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home),
           "FAKE": str(fake), "XDG_RUNTIME_DIR": str(runtime)}
    box = Solar(fake, runtime, env)
    box.procs(*STEAMVR)
    box.dies_on("quit")
    return box


def test_quit_asks_vrmonitor_and_stops_there(solar: Solar) -> None:
    (solar.runtime / "psvr2-steamvr.last").write_text("123", encoding="utf-8")

    result = solar.run("quit")

    assert result.returncode == 0
    assert "SteamVR quit" in result.stdout
    assert "vrmonitor.sh vrmonitor://quit" in solar.log
    assert not [line for line in solar.log if line.startswith("pkill")]
    assert not (solar.runtime / "psvr2-steamvr.last").exists()


def test_quit_falls_back_to_sigterm(solar: Solar) -> None:
    solar.dies_on("TERM")

    result = solar.run("quit")

    assert result.returncode == 0
    assert "SteamVR exited on SIGTERM" in result.stdout
    assert "sleep 1" in solar.log
    assert [line.split()[1] for line in solar.log if line.startswith("pkill")] == ["-TERM"] * 5


def test_quit_escalates_to_sigkill(solar: Solar) -> None:
    solar.dies_on("KILL")

    result = solar.run("quit")

    assert result.returncode == 0
    assert "SteamVR killed" in result.stdout
    assert {line.split()[1] for line in solar.log if line.startswith("pkill")} == {"-TERM", "-KILL"}


def test_quit_fails_when_steamvr_survives_sigkill(solar: Solar) -> None:
    solar.dies_on("never")

    result = solar.run("quit")

    assert result.returncode == 1
    assert "still running after SIGKILL" in result.stderr
    assert "vrserver" in result.stderr


def test_quit_without_vrmonitor_goes_straight_to_sigterm(solar: Solar) -> None:
    solar.procs("vrserver")
    solar.dies_on("TERM")

    result = solar.run("quit")

    assert result.returncode == 0
    assert "vrmonitor not running" in result.stdout
    assert not [line for line in solar.log if line.startswith("vrmonitor.sh")]
    assert solar.log.count("sleep 1") == 0


def test_quit_with_steamvr_down_does_nothing(solar: Solar) -> None:
    solar.procs()

    result = solar.run("quit")

    assert (result.returncode, result.stdout) == (0, "SteamVR not running\n")
    assert solar.log == []


def test_stop_leaves_steamvr_when_the_headset_came_back(solar: Solar) -> None:
    solar.headset(True)

    result = solar.run("stop")

    assert result.returncode == 0
    assert "leaving SteamVR running" in result.stdout
    assert solar.log == ["sleep 10"]


def test_stop_restarts_steamvr_with_a_crashed_overlay(solar: Solar) -> None:
    solar.headset(True)
    solar.procs("vrmonitor", "vrcompositor", "vrserver")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "overlay has crashed" in result.stdout
    assert "SteamVR quit" in result.stdout
    assert not [line for line in solar.log if line.startswith(("steam", "systemd-run"))]


def test_stop_ignores_a_stray_overlay_when_steamvr_is_down(solar: Solar) -> None:
    solar.headset(True)
    solar.procs("vrwebhelper")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "SteamVR not running, leaving it to start" in result.stdout


def test_stop_skips_the_grace_during_shutdown(solar: Solar) -> None:
    solar.headset(True)
    (solar.fake / "system_state").write_text("stopping\n", encoding="utf-8")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "system shutting down" in result.stdout
    assert "sleep 10" not in solar.log
    assert "SteamVR quit" in result.stdout


def test_stop_restarts_steam_whose_webhelper_crashed(solar: Solar) -> None:
    (solar.fake / "ps_helper").write_text("999999 3 /x/steamwebhelper\n", encoding="utf-8")
    (solar.fake / "ps_client").write_text("  4000\n", encoding="utf-8")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "respawned, restarting Steam" in result.stdout
    assert "steam -shutdown" in solar.log
    launches = [line for line in solar.log if line.startswith("systemd-run")]
    assert len(launches) == 1
    assert "rungameid" not in launches[0]


def test_stop_leaves_a_healthy_steam_alone(solar: Solar) -> None:
    (solar.fake / "ps_helper").write_text("999999 3990 /x/steamwebhelper\n", encoding="utf-8")
    (solar.fake / "ps_client").write_text("4000\n", encoding="utf-8")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "steam -shutdown" not in solar.log
    assert f"sleep {5}" in solar.log


def test_stop_does_not_restart_steam_after_a_failed_quit(solar: Solar) -> None:
    solar.dies_on("never")
    (solar.fake / "ps_helper").write_text("999999 3 /x/steamwebhelper\n", encoding="utf-8")
    (solar.fake / "ps_client").write_text("4000\n", encoding="utf-8")

    result = solar.run("stop")

    assert result.returncode == 1
    assert "steam -shutdown" not in solar.log


def test_start_after_a_quit_is_not_held_by_the_cooldown(solar: Solar) -> None:
    solar.dies_on("quit")
    assert solar.run("quit").returncode == 0

    result = solar.run("start")

    assert "starting SteamVR" in result.stdout
    assert any("steam://rungameid/250820" in line for line in solar.log)
