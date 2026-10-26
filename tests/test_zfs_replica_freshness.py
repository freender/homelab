"""Replica freshness: zfs-pool-textfile-exporter's replica series and its inventory.

The exporter is run for real against stubbed `zpool`/`zfs`; the orchestrator side
is checked against inline inventories so the dataset list provably comes from
zfs-automation's own push-target config, template references included.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from homelab.hosts import HostRegistry
from homelab.modules import metrics_exporters

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "metrics-exporters" / "configs" / "common" / "zfs-pool-textfile-exporter"
METRIC = "homelab_zfs_replica_last_snapshot_timestamp_seconds"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKfdTyEwNoO68zyFoxYZ836yaE1QyY5YbGXS6fjnd9an t"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def run_exporter(
    tmp_path: Path, snapshots: dict[str, list[int]], replicas: str | None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """`snapshots` maps an existing dataset to its snapshot creation times."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    out_dir = tmp_path / "textfile"
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    for dataset, times in snapshots.items():
        (snap_dir / dataset.replace("/", "__")).write_text(
            "".join(f"{value}\n" for value in times), encoding="utf-8"
        )

    _write_exec(stub_dir / "zpool", "#!/bin/bash\nexit 0\n")
    # Only `zfs list ... -d 1 <dataset>` is used; the dataset is the last argument.
    # A dataset with no file does not exist, like the real `zfs list` erroring out.
    _write_exec(
        stub_dir / "zfs",
        f"""#!/bin/bash
file="{snap_dir}/${{!#//\\//__}}"
[[ -f "$file" ]] || {{ echo "cannot open '${{!#}}': dataset does not exist" >&2; exit 1; }}
cat "$file"
""",
    )
    _write_exec(stub_dir / "hostname", '#!/bin/bash\nprintf "%s\\n" "testhost"\n')

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["TEXTFILE_DIR"] = str(out_dir)
    env["ZFS_EXPECTED_POOLS_FILE"] = str(tmp_path / "absent-pools.conf")
    replicas_file = tmp_path / "zfs-replica-datasets.conf"
    if replicas is not None:
        replicas_file.write_text(replicas, encoding="utf-8")
    env["ZFS_REPLICA_DATASETS_FILE"] = str(replicas_file)

    result = subprocess.run(
        ["bash", str(EXPORTER)], capture_output=True, text=True, env=env, check=False
    )
    return result, out_dir / "zfs-pools.prom"


def replica_values(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if line.startswith(f"{METRIC}{{"):
            labels = line[line.index("{") + 1 : line.index("}")]
            dataset = labels.split('dataset="', 1)[1].split('"', 1)[0]
            values[dataset] = line.rsplit(" ", 1)[1]
    return values


def test_newest_snapshot_creation_is_reported(tmp_path: Path) -> None:
    result, prom = run_exporter(
        tmp_path,
        {"vault-hdd/replica/xur-datastore": [1793000000, 1793036064]},
        "# comment\nvault-hdd/replica/xur-datastore\n",
    )
    assert result.returncode == 0, result.stderr
    text = prom.read_text(encoding="utf-8")
    assert replica_values(text) == {"vault-hdd/replica/xur-datastore": "1793036064"}
    assert (
        f'{METRIC}{{host="testhost",zpool="vault-hdd",'
        'dataset="vault-hdd/replica/xur-datastore"} 1793036064'
    ) in text


def test_missing_dataset_and_empty_dataset_report_zero(tmp_path: Path) -> None:
    """Absent would be unmatchable by the staleness rule; 0 makes it the stalest."""
    result, prom = run_exporter(
        tmp_path,
        {"vault-hdd/replica/seeding": []},
        "vault-hdd/replica/seeding\nvault-hdd/replica/destroyed\n",
    )
    assert result.returncode == 0, result.stderr
    assert replica_values(prom.read_text(encoding="utf-8")) == {
        "vault-hdd/replica/seeding": "0",
        "vault-hdd/replica/destroyed": "0",
    }


def test_no_replica_file_emits_no_replica_series(tmp_path: Path) -> None:
    result, prom = run_exporter(tmp_path, {}, None)
    assert result.returncode == 0, result.stderr
    assert replica_values(prom.read_text(encoding="utf-8")) == {}


CONFIG = {"hostname": "h.invalid", "user": "root", "sshkey": "infra"}


def write_inventory(tmp_path: Path, hosts: dict) -> Path:
    for name, host in hosts.items():
        host["config"] = {**CONFIG, "hostname": f"{name}.invalid", **host["config"]}
    (tmp_path / "hosts.conf").write_text(yaml.safe_dump(hosts), encoding="utf-8")
    return tmp_path


def patch_registry(monkeypatch, root: Path) -> None:
    registry = HostRegistry(root / "hosts.conf")
    monkeypatch.setattr(metrics_exporters, "default_registry", lambda _root: registry)


def test_replica_datasets_come_from_push_target_access(tmp_path: Path, monkeypatch) -> None:
    """Template references resolve the same way zfs-automation resolves them."""
    root = write_inventory(
        tmp_path,
        {
            "ace": {
                "config": {"type": "pve"},
                "features": {
                    "zfs-automation": {
                        "push_target_access_templates": {
                            "neo-data": {
                                "datasets": ["cache/neo/pictures"],
                                "allowed_pushers": [
                                    {"name": "bray", "from": "*", "public_key": KEY}
                                ],
                            }
                        },
                        "push_target_access": {
                            "datasets": ["vault-hdd/replica/a", "vault-hdd/replica/b"],
                            "allowed_pushers": [
                                {"name": "osiris", "from": "10.0.0.80", "public_key": KEY}
                            ],
                        },
                    }
                },
            },
            "cinci": {
                "config": {"type": "ubuntu"},
                "features": {
                    "zfs-automation": {"push_target_access": {"template": "ace:neo-data"}}
                },
            },
            "bray": {"config": {"type": "pve"}, "features": {"zfs-automation": {}}},
            "off": {
                "config": {"type": "pve"},
                "features": {
                    "zfs-automation": {
                        "push_target_access": {"template": "ace:neo-data", "enabled": False}
                    }
                },
            },
        },
    )
    patch_registry(monkeypatch, root)
    assert metrics_exporters.zfs_replica_datasets(root, "ace") == [
        "vault-hdd/replica/a",
        "vault-hdd/replica/b",
    ]
    assert metrics_exporters.zfs_replica_datasets(root, "cinci") == ["cache/neo/pictures"]
    assert metrics_exporters.zfs_replica_datasets(root, "bray") == []
    assert metrics_exporters.zfs_replica_datasets(root, "off") == []


def test_replica_datasets_are_empty_in_a_guest(tmp_path: Path, monkeypatch) -> None:
    root = write_inventory(tmp_path, {"g": {"config": {"type": "ubuntu"}, "features": {}}})
    patch_registry(monkeypatch, root)
    assert metrics_exporters.bare_metal_data(root, "g", lxc_guest=True).replica_datasets == []


def test_replica_conf_is_deployed_only_when_there_are_replicas() -> None:
    def names(**flags) -> set[str]:
        options = {
            "has_apcupsd": False,
            "has_igpu": False,
            "has_hba": False,
            "has_expected_pools": False,
            "has_disk_label_overrides": False,
            "has_pve_patch_statuses": False,
            "has_pve_replication": False,
            "has_wrapper": False,
            "lxc_guest": False,
            "own_kernel": True,
        }
        options.update(flags)
        return {spec.build_name for spec in metrics_exporters.build_file_specs(**options)}

    conf = "zfs-replica-datasets.conf"
    assert conf in names(has_replica_datasets=True)
    assert conf not in names()
    assert conf not in names(has_replica_datasets=True, lxc_guest=True)


def test_rendered_conf_lists_each_dataset(tmp_path: Path) -> None:
    out = tmp_path / "zfs-replica-datasets.conf"
    metrics_exporters.render_if_any(
        ["vault-hdd/replica/a", "cache/neo/pictures"],
        metrics_exporters.zfs_replica_datasets_template(ROOT),
        out,
        "ZFS_REPLICA_DATASETS",
    )
    lines = [
        line.strip()
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == ["vault-hdd/replica/a", "cache/neo/pictures"]
