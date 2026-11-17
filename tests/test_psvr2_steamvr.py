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
pat= list= full=
while (( $# )); do
    case "$1" in -x) pat=$2; shift ;; -f) full=1 ;; -l) list=1 ;; -u) shift ;; *) pat=$1 ;; esac
    shift
done
rc=1
while read -r p; do
    if [[ -n "$full" ]]; then
        [[ -n "$p" && "$p" == *"$pat"* ]] || continue
    else
        [[ -n "$p" && "$p" =~ ^($pat)$ ]] || continue
    fi
    rc=0
    [[ -n "$list" ]] && echo "1 $p"
done < "$FAKE/procs"
exit "$rc"
""",
    "pkill": r"""
sig= name=
for a in "$@"; do case "$a" in -TERM|-KILL) sig=$a ;; -*|[0-9]*) ;; *) name=$a ;; esac; done
if [[ "$name" == steam* ]]; then
    echo "pkill $sig $name" >> "$FAKE/log"
    [[ "$name" == steam && "$(cat "$FAKE/steam_dies_on" 2>/dev/null)" == "${sig#-}" ]] \
        && sed -i '/^steam$/d' "$FAKE/procs"
    exit 0
fi
echo "pkill $sig" >> "$FAKE/log"
[[ "$(cat "$FAKE/dies_on")" == "${sig#-}" ]] && sed -i '/^vr/d' "$FAKE/procs"
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
    *comm=*) cat "$FAKE/ps_comm" 2>/dev/null ;;
    *) cat "$FAKE/ps_client" 2>/dev/null || exit 1 ;;
esac
""",
    "sleep": r"""
echo "sleep $*" >> "$FAKE/log"
if [[ -e "$FAKE/headset_after" ]]; then
    left=$(( $(cat "$FAKE/headset_after") - 1 ))
    echo "$left" > "$FAKE/headset_after"
    (( left > 0 )) || touch "$FAKE/headset"
fi
""",
    "flock": "exit 0",
    "timeout": 'shift; exec "$@"',
    "kscreen-doctor": 'echo "kscreen-doctor $*" >> "$FAKE/log"',
    "systemd-run": 'echo "systemd-run $*" >> "$FAKE/log"',
    "steam": r"""
echo "steam $*" >> "$FAKE/log"
[[ "$*" == -shutdown && ! -e "$FAKE/steam_ignores_shutdown" ]] && sed -i '/^steam$/d' "$FAKE/procs"
exit 0
""",
}

VRMONITOR = r"""#!/bin/bash
echo "vrmonitor.sh $*" >> "$FAKE/log"
[[ "$(cat "$FAKE/dies_on")" == quit ]] && sed -i "/^vr/d" "$FAKE/procs"
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

    def connection_log(self, *lines: str) -> None:
        path = Path(self.env["HOME"]) / ".local/share/Steam/logs/connection_log.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def steam_client(self) -> None:
        """A running Steam whose webhelper started with it, so it was never respawned."""
        (self.fake / "ps_helper").write_text("999999 3990 /x/steamwebhelper\n", encoding="utf-8")
        (self.fake / "ps_client").write_text("4000\n", encoding="utf-8")

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


REQUEUE = ("systemd-run --user --collect --no-block --quiet --on-active=3 "
           "--timer-property=AccuracySec=100ms systemctl --user start psvr2-steamvr.service")


def test_stop_leaves_steamvr_when_the_headset_came_back(solar: Solar) -> None:
    solar.headset(True)

    result = solar.run("stop")

    assert result.returncode == 0
    assert "leaving SteamVR running" in result.stdout
    assert solar.log == [REQUEUE]


def test_stop_restarts_steamvr_with_a_crashed_overlay(solar: Solar) -> None:
    solar.headset(True)
    solar.procs("vrmonitor", "vrcompositor", "vrserver")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "overlay has crashed" in result.stdout
    assert "SteamVR quit" in result.stdout
    assert not [line for line in solar.log if line.startswith("steam")]
    assert solar.log.index("vrmonitor.sh vrmonitor://quit") < solar.log.index(REQUEUE)


def test_stop_ignores_a_stray_overlay_when_steamvr_is_down(solar: Solar) -> None:
    solar.headset(True)
    solar.procs("vrwebhelper")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "SteamVR not running, leaving it to start" in result.stdout
    assert solar.log == [REQUEUE]


def test_stop_does_not_requeue_once_the_headset_is_gone(solar: Solar) -> None:
    result = solar.run("stop")

    assert result.returncode == 0
    assert REQUEUE not in solar.log
    assert solar.log[:10] == ["sleep 1"] * 10
    assert "SteamVR quit" in result.stdout


def test_stop_ends_the_grace_when_the_headset_returns(solar: Solar) -> None:
    (solar.fake / "headset_after").write_text("2", encoding="utf-8")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "leaving SteamVR running" in result.stdout
    assert solar.log == ["sleep 1", "sleep 1", REQUEUE]


def test_stop_does_not_requeue_after_a_failed_quit(solar: Solar) -> None:
    solar.headset(True)
    solar.procs("vrmonitor", "vrserver")
    solar.dies_on("never")

    result = solar.run("stop")

    assert result.returncode == 1
    assert REQUEUE not in solar.log


def test_stop_skips_the_grace_during_shutdown(solar: Solar) -> None:
    solar.headset(True)
    (solar.fake / "system_state").write_text("stopping\n", encoding="utf-8")

    result = solar.run("stop")

    assert result.returncode == 0
    assert "system shutting down" in result.stdout
    assert "sleep 1" not in solar.log
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


LOGGED_ON = ("[2026-11-16 13:27:20] [Logged On, 4, 7] [U:1:51555675] "
             "RecvMsgClientLogOnResponse() : processing complete")
LOGGED_OFF = "[2026-11-14 09:17:35] [Logged Off, 0, 0] [U:1:0] CCMInterface::SetSteamID( [U:1:0] )"
SIGNED_OUT = "[2026-11-15 15:07:53] [Logged Off, 0, 0] [U:1:51555675] Log session ended"


def test_start_restarts_a_steam_that_is_not_logged_in(solar: Solar) -> None:
    solar.procs()
    solar.steam_client()
    restarted = "[2026-11-16 18:00:00] Client version: 1794536281"
    solar.connection_log(LOGGED_ON, "", restarted, LOGGED_OFF)

    result = solar.run("start")

    assert result.returncode == 0
    assert "Steam is not logged in, restarting it" in result.stdout
    assert solar.log.index("steam -shutdown") < next(
        i for i, line in enumerate(solar.log) if "steam://rungameid/250820" in line)


@pytest.mark.parametrize("last", [LOGGED_ON, SIGNED_OUT], ids=["logged-on", "known-account"])
def test_start_leaves_a_steam_with_an_account_alone(solar: Solar, last: str) -> None:
    solar.procs()
    solar.steam_client()
    solar.connection_log(LOGGED_OFF, last)

    result = solar.run("start")

    assert result.returncode == 0
    assert "not logged in" not in result.stdout
    assert "steam -shutdown" not in solar.log
    assert any("steam://rungameid/250820" in line for line in solar.log)


@pytest.mark.parametrize("client", [False, True], ids=["no-steam", "no-log"])
def test_start_without_a_logged_off_steam_just_launches(solar: Solar, client: bool) -> None:
    solar.procs()
    if client:
        solar.steam_client()
    else:
        solar.connection_log(LOGGED_OFF)

    result = solar.run("start")

    assert result.returncode == 0
    assert "steam -shutdown" not in solar.log
    assert any("steam://rungameid/250820" in line for line in solar.log)


def test_start_relaunches_steamvr_whose_compositor_died(solar: Solar) -> None:
    solar.procs("vrmonitor", "vrserver")
    (solar.fake / "ps_comm").write_text("  840 vrserver\n", encoding="utf-8")
    (solar.runtime / "psvr2-steamvr.last").write_text(str(10**10), encoding="utf-8")

    result = solar.run("start")

    assert result.returncode == 0
    assert "running without its compositor" in result.stdout
    assert solar.log.index("vrmonitor.sh vrmonitor://quit") < next(
        i for i, line in enumerate(solar.log) if "steam://rungameid/250820" in line)


@pytest.mark.parametrize(("procs", "age"), [
    (("vrmonitor", "vrcompositor", "vrserver"), "840"),
    (("vrmonitor", "vrserver"), "30"),
], ids=["compositor-up", "still-starting"])
def test_start_adopts_a_steamvr_it_should_not_restart(solar: Solar, procs: tuple[str, ...],
                                                     age: str) -> None:
    solar.procs(*procs)
    (solar.fake / "ps_comm").write_text(f"{age} vrserver\n", encoding="utf-8")

    result = solar.run("start")

    assert (result.returncode, result.stdout) == (0, "SteamVR already running\n")
    assert solar.log == []


def test_start_after_a_quit_is_not_held_by_the_cooldown(solar: Solar) -> None:
    solar.dies_on("quit")
    assert solar.run("quit").returncode == 0

    result = solar.run("start")

    assert "starting SteamVR" in result.stdout
    assert any("steam://rungameid/250820" in line for line in solar.log)


# Logs "edit <how many Steam processes were running>", then exits with its argument.
EDIT = ("bash", "-c",
        'echo "edit $(grep -c "^steam$" "$FAKE/procs")" >> "$FAKE/log"; exit "${1:-0}"', "_")


def launches(solar: Solar) -> list[str]:
    return [line for line in solar.log if line.startswith("systemd-run")]


def test_steam_edit_runs_the_command_with_steam_quit_then_relaunches_it(solar: Solar) -> None:
    solar.procs("steam")

    result = solar.run("steam-edit", "--", *EDIT)

    assert result.returncode == 0, result.stderr
    assert solar.log.index("steam -shutdown") < solar.log.index("edit 0")
    assert launches(solar) == ["systemd-run --user --collect --no-block --quiet steam -pipewire"]
    assert solar.log.index("edit 0") < solar.log.index(launches(solar)[0])


def test_steam_edit_leaves_a_stopped_steam_stopped(solar: Solar) -> None:
    solar.procs()

    result = solar.run("steam-edit", "--", *EDIT)

    assert result.returncode == 0
    assert solar.log == ["edit 0"]


def test_steam_edit_relaunches_steam_after_a_failed_command(solar: Solar) -> None:
    solar.procs("steam")

    result = solar.run("steam-edit", "--", *EDIT, "7")

    assert result.returncode == 7
    assert len(launches(solar)) == 1


@pytest.mark.parametrize(("procs", "code", "message"), [
    (("steam", "reaper SteamLaunch AppId=620 -- game"), 3, "a Steam game is running"),
    (("steam", *STEAMVR), 4, "pass --quit-vr"),
], ids=["game", "steamvr"])
def test_steam_edit_refuses_while_in_use(solar: Solar, procs: tuple[str, ...], code: int,
                                         message: str) -> None:
    solar.procs(*procs)

    result = solar.run("steam-edit", "--", *EDIT)

    assert result.returncode == code
    assert message in result.stderr
    assert solar.log == []


def test_steam_edit_quits_steamvr_and_brings_it_back_with_the_headset_on(solar: Solar) -> None:
    solar.procs("steam", *STEAMVR)
    solar.headset(True)

    result = solar.run("steam-edit", "--quit-vr", "--", *EDIT)

    assert result.returncode == 0, result.stderr
    order = [solar.log.index(step) for step in
             ("vrmonitor.sh vrmonitor://quit", "steam -shutdown", "edit 0")]
    assert order == sorted(order)
    assert launches(solar) == [
        "systemd-run --user --collect --no-block --quiet steam -pipewire steam://rungameid/250820"]


def test_steam_edit_launches_steamvr_held_off_by_the_lock(solar: Solar) -> None:
    solar.procs("steam")
    solar.headset(True)

    result = solar.run("steam-edit", "--", *EDIT)

    assert result.returncode == 0
    assert "relaunching Steam with SteamVR" in result.stdout
    assert "steam://rungameid/250820" in launches(solar)[0]


def test_steam_edit_does_not_run_the_command_when_steam_survives(solar: Solar) -> None:
    solar.procs("steam")
    (solar.fake / "steam_ignores_shutdown").touch()
    (solar.fake / "steam_dies_on").write_text("never", encoding="utf-8")

    result = solar.run("steam-edit", "--", *EDIT)

    assert result.returncode == 1
    assert "Steam still running after SIGKILL" in result.stderr
    assert not [line for line in solar.log if line.startswith("edit")]
    assert {"pkill -TERM steam", "pkill -KILL steamwebhelper", "pkill -KILL steam"} <= set(
        solar.log)


def test_steam_edit_terminates_a_steam_that_ignores_shutdown(solar: Solar) -> None:
    solar.procs("steam")
    (solar.fake / "steam_ignores_shutdown").touch()
    (solar.fake / "steam_dies_on").write_text("TERM", encoding="utf-8")

    result = solar.run("steam-edit", "--", *EDIT)

    assert result.returncode == 0
    assert "Steam ignored -shutdown" in result.stdout
    assert "edit 0" in solar.log


def test_steam_edit_needs_a_command(solar: Solar) -> None:
    result = solar.run("steam-edit", "--quit-vr", "--")

    assert result.returncode == 2
    assert "usage" in result.stderr


def test_steam_restart_launches_steam_even_when_it_was_down(solar: Solar) -> None:
    solar.procs()

    result = solar.run("steam-restart")

    assert result.returncode == 0
    assert launches(solar) == ["systemd-run --user --collect --no-block --quiet steam -pipewire"]


def test_steam_restart_refuses_with_steamvr_up(solar: Solar) -> None:
    result = solar.run("steam-restart")

    assert result.returncode == 4
    assert launches(solar) == []
