from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_docker_update_service_retries_transient_failures() -> None:
    service = (ROOT / "docker" / "templates" / "homelab-docker-update.service").read_text(
        encoding="utf-8"
    )

    assert "Restart=on-failure" in service
    assert "RestartSec=10min" in service
    assert "StartLimitIntervalSec=1h" in service
    assert "StartLimitBurst=3" in service


def prepare_start_root(tmp_path: Path) -> tuple[Path, Path]:
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    shutil.copy(ROOT / "docker" / "scripts" / "start.sh", appdata / "start.sh")
    shutil.copy(
        ROOT / "docker" / "scripts" / "docker-common.sh",
        appdata / "docker-common.sh",
    )

    stack = appdata / "example"
    stack.mkdir()
    (stack / "compose.yml").write_text(
        "services:\n  example:\n    image: example\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/bash\n"
        "set -u\n"
        "printf '%s\\n' \"$*\" >> \"${DOCKER_LOG:?}\"\n"
        "if [[ \"$1\" == \"compose\" && \"${2:-}\" == \"config\" ]]; then\n"
        "    if [[ -f config.json ]]; then cat config.json; else echo '{\"name\": \"x\"}'; fi\n"
        "    exit 0\n"
        "fi\n"
        "if [[ \"$1\" == \"compose\" ]]; then\n"
        "    if [[ \"${DOCKER_FAIL_PULL:-false}\" == \"true\" && \"${2:-}\" == \"pull\" ]]; then\n"
        "        exit 7\n"
        "    fi\n"
        "    exit 0\n"
        "fi\n"
        "if [[ \"$1\" == \"network\" && \"${2:-}\" == \"inspect\" ]]; then\n"
        "    [[ \" ${DOCKER_MISSING_NETWORKS:-} \" == *\" ${3:-} \"* ]] && exit 1\n"
        "    exit 0\n"
        "fi\n"
        "if [[ \"$1\" == \"ps\" ]]; then\n"
        "    printf '%b' \"${DOCKER_PS_OUTPUT:-}\"\n"
        "    exit 0\n"
        "fi\n"
        "if [[ \"$1\" == \"image\" && \"${2:-}\" == \"prune\" ]]; then\n"
        "    exit 0\n"
        "fi\n"
        "if [[ \"$1\" == \"system\" && \"${2:-}\" == \"prune\" ]]; then\n"
        "    exit 99\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    return appdata, fake_bin


def run_start(
    tmp_path: Path,
    args: list[str] | None = None,
    *,
    ps_output: str = "",
    fail_pull: bool = False,
    missing_networks: tuple[str, ...] = (),
    stacks: dict[str, dict[str, object]] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    appdata, fake_bin = prepare_start_root(tmp_path)
    for name, config in (stacks or {}).items():
        stack = appdata / name
        stack.mkdir(exist_ok=True)
        (stack / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        (stack / "config.json").write_text(json.dumps({"name": name, **config}), encoding="utf-8")
    log = tmp_path / "docker.log"
    # start.sh runs a python3 child; same reasoning as test_safety_regressions.py:
    # pytest-cov's .pth would start un-branched coverage there and break the
    # end-of-session combine.
    inherited = {key: value for key, value in os.environ.items() if "COV_CORE" not in key}
    for name in ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG", "MUTANT_UNDER_TEST"):
        inherited.pop(name, None)
    env = {
        **inherited,
        "DOCKER_LOG": str(log),
        "DOCKER_PS_OUTPUT": ps_output,
        "DOCKER_FAIL_PULL": "true" if fail_pull else "false",
        "DOCKER_MISSING_NETWORKS": " ".join(missing_networks),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        # start.sh defaults to PULL_RETRY_DELAY=15, so the failing-pull case slept
        # for two real 15s intervals and was ~95% of the entire suite's runtime.
        # The retry *count* is what the tests assert on (via the command log), and
        # PULL_ATTEMPTS is left at its default so that behavior is unchanged.
        "PULL_RETRY_DELAY": "0",
    }

    result = subprocess.run(
        ["bash", str(appdata / "start.sh"), *(args or [])],
        cwd=appdata,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    commands = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, commands


def test_start_prunes_images_after_default_update_run(tmp_path: Path) -> None:
    result, commands = run_start(tmp_path)

    assert result.returncode == 0
    assert "compose pull" in commands
    assert "compose up -d" in commands
    assert "image prune -af" in commands
    assert all("system prune" not in command for command in commands)


def test_start_skips_auto_prune_for_no_pull_run(tmp_path: Path) -> None:
    result, commands = run_start(tmp_path, ["--no-pull"])

    assert result.returncode == 0
    assert "compose up -d" in commands
    assert "image prune -af" not in commands
    assert ">>> Skipping Docker image prune (--no-pull run)" in result.stdout


def test_start_skips_prune_when_stopped_containers_exist(tmp_path: Path) -> None:
    result, commands = run_start(
        tmp_path,
        ps_output="old-container: Exited (0) 2 hours ago\n",
    )

    assert result.returncode == 0
    assert "image prune -af" not in commands
    assert ">>> Skipping Docker image prune because stopped containers exist" in result.stdout
    assert "old-container: Exited (0) 2 hours ago" in result.stdout


def test_start_skips_prune_when_stack_fails(tmp_path: Path) -> None:
    result, commands = run_start(tmp_path, fail_pull=True)

    assert result.returncode == 1
    assert "image prune -af" not in commands
    assert ">>> Skipping Docker image prune because one or more stacks failed" in result.stdout


def network_creates(commands: list[str]) -> list[str]:
    return [command for command in commands if command.startswith("network create")]


ONSITE_STACKS: dict[str, dict[str, object]] = {
    "traefik": {"networks": {"net.internal": {"name": "net.internal", "ipam": {}}}},
    "example": {"networks": {"net.internal": {"name": "net.internal", "external": True}}},
}


def test_start_recreates_missing_shared_network_from_its_owner(tmp_path: Path) -> None:
    result, commands = run_start(
        tmp_path, stacks=ONSITE_STACKS, missing_networks=("net.internal",)
    )

    assert result.returncode == 0
    assert network_creates(commands) == [
        "network create --label com.docker.compose.project=traefik"
        " --label com.docker.compose.network=net.internal net.internal"
    ]
    # After the owner's priority start, before any joiner.
    create = commands.index(network_creates(commands)[0])
    up_runs = [i for i, command in enumerate(commands) if command == "compose up -d"]
    assert up_runs[0] < create < up_runs[1]


def test_start_keeps_owner_subnet_when_recreating(tmp_path: Path) -> None:
    owned = {
        "name": "bridge.example",
        "ipam": {"config": [{"subnet": "10.10.0.0/24", "gateway": "10.10.0.1"}]},
        "enable_ipv6": False,
    }
    stacks: dict[str, dict[str, object]] = {
        "aaa": {"networks": {"shared": {"name": "bridge.example", "external": True}}},
        "zzz": {"networks": {"shared": owned}},
    }
    result, commands = run_start(tmp_path, stacks=stacks, missing_networks=("bridge.example",))

    assert result.returncode == 0
    assert network_creates(commands) == [
        "network create --label com.docker.compose.project=zzz"
        " --label com.docker.compose.network=shared"
        " --subnet 10.10.0.0/24 --gateway 10.10.0.1 bridge.example"
    ]


def test_start_leaves_existing_shared_network_alone(tmp_path: Path) -> None:
    result, commands = run_start(tmp_path, stacks=ONSITE_STACKS)

    assert result.returncode == 0
    assert network_creates(commands) == []


def test_start_never_creates_a_network_without_an_owner_stack(tmp_path: Path) -> None:
    stacks: dict[str, dict[str, object]] = {
        "example": {"networks": {"net_overlay": {"name": "net_overlay", "external": True}}},
    }
    result, commands = run_start(tmp_path, stacks=stacks, missing_networks=("net_overlay",))

    assert result.returncode == 0
    assert network_creates(commands) == []
