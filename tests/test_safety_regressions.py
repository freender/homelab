from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from homelab import cli
from homelab.modules import (
    apt_upgrade,
    keepalived,
    pve_gpu_passthrough,
    pve_http_boot,
    pve_postinstall_webhook,
)

ROOT = Path(__file__).resolve().parents[1]


class ValueRegistry:
    def __init__(self, value: object) -> None:
        self.value = value

    def get(self, _host: str, _key: str, default: object = None) -> object:
        return self.value if self.value is not None else default


@pytest.mark.parametrize(
    "normalizer",
    [
        apt_upgrade.normalize_autoupgrade,
        pve_gpu_passthrough.normalize_isolate_host_gpu,
        pve_http_boot._wants_iso,
        pve_postinstall_webhook.normalize_webhook_dry_run,
    ],
)
def test_dangerous_boolean_settings_are_strict(normalizer) -> None:
    assert normalizer(ValueRegistry("false"), "ace") is False
    assert normalizer(ValueRegistry("true"), "ace") is True
    with pytest.raises(ValueError, match="must be true or false"):
        normalizer(ValueRegistry("ture"), "ace")


def test_deploy_rejects_unknown_host_before_module_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "hosts.conf").write_text(
        """
ace:
  config:
    type: pve
    hostname: ace.internal
    user: root
    sshkey: infra
  features: {}
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)

    result = CliRunner().invoke(cli.main, ["deploy", "docker", "orbit"])

    assert result.exit_code != 0
    assert "unknown host 'orbit'" in result.output


def test_pause_reports_systemd_disable_failure() -> None:
    script = f"""
source {shlex.quote(str(ROOT / 'lib' / 'utils.sh'))}
systemctl() {{
    case "$1" in
        is-active) return 0 ;;
        is-enabled) return 1 ;;
        disable) return 1 ;;
    esac
}}
homelab_apply_pause true homelab-test.timer
"""

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "failed to stop and disable homelab-test.timer" in result.stderr


def test_retire_systemd_unit_stops_and_removes_unit(tmp_path: Path) -> None:
    unit_path = tmp_path / "homelab-test.timer"
    unit_path.write_text("[Timer]\n", encoding="utf-8")
    log_path = tmp_path / "systemctl.log"
    script = f"""
source {shlex.quote(str(ROOT / 'lib' / 'utils.sh'))}
systemctl() {{
    printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
    case "$1" in
        is-enabled) return 0 ;;
        *) return 0 ;;
    esac
}}
retire_systemd_unit homelab-test.timer "$UNIT_PATH"
"""
    env = {
        **os.environ,
        "SYSTEMCTL_LOG": str(log_path),
        "UNIT_PATH": str(unit_path),
    }

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not unit_path.exists()
    log = log_path.read_text(encoding="utf-8")
    assert "disable --now homelab-test.timer" in log
    assert "daemon-reload" in log


def test_ssh_config_installer_propagates_copy_failure(tmp_path: Path) -> None:
    module_dir = tmp_path / "ssh-config"
    scripts_dir = module_dir / "scripts"
    lib_dir = module_dir / "lib"
    build_dir = module_dir / "build" / "ace"
    scripts_dir.mkdir(parents=True)
    lib_dir.mkdir()
    build_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "ssh-config" / "scripts" / "install.sh", scripts_dir / "install.sh")
    (build_dir / "config").write_text("Host ace\n", encoding="utf-8")
    (lib_dir / "utils.sh").write_text(
        """
require_file() { return 0; }
print_header() { :; }
backup_and_copy_if_changed() { return 2; }
""".lstrip(),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        ["bash", str(scripts_dir / "install.sh"), "ace"],
        cwd=module_dir,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2


def test_keepalived_build_uses_caller_supplied_tmpfs_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = {
        "config.type": "ubuntu",
        "keepalived.interface": "eth0",
        "keepalived.instance_name": "test",
        "keepalived.healthcheck_host_env": "HEALTHCHECK_HOST",
        "keepalived.healthcheck_url_env": "HEALTHCHECK_URL",
        "keepalived.unicast_src_ip": "10.0.0.10",
        "keepalived.virtual_router_id": 10,
        "keepalived.priority": 100,
        "keepalived.advert_interval": 1,
        "keepalived.preempt_delay": 0,
        "keepalived.unicast_peers": ["10.0.0.11"],
        "keepalived.virtual_ips": ["10.0.0.15/24"],
    }

    class Registry:
        def get(self, _host: str, key: str, default: object = None) -> object:
            return values.get(key, default)

    monkeypatch.setattr(keepalived, "default_registry", lambda _root: Registry())
    monkeypatch.setattr(
        keepalived,
        "load_keepalived_env",
        lambda _root: {
            "HEALTHCHECK_HOST": "route.example.net",
            "HEALTHCHECK_URL": "https://route.example.net/ping",
        },
    )
    build_dir = tmp_path / "tmpfs-stage"

    artifacts = keepalived.build_host_artifacts(ROOT, "safety-test", build_dir)

    assert artifacts.build_dir == build_dir
    assert "route.example.net" in (build_dir / "healthcheck.sh").read_text(encoding="utf-8")
    assert not (ROOT / "keepalived" / "build" / "safety-test").exists()
