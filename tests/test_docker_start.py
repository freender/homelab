from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        "if [[ \"$1\" == \"compose\" ]]; then\n"
        "    if [[ \"${DOCKER_FAIL_PULL:-false}\" == \"true\" && \"${2:-}\" == \"pull\" ]]; then\n"
        "        exit 7\n"
        "    fi\n"
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
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    appdata, fake_bin = prepare_start_root(tmp_path)
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "DOCKER_LOG": str(log),
        "DOCKER_PS_OUTPUT": ps_output,
        "DOCKER_FAIL_PULL": "true" if fail_pull else "false",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
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
