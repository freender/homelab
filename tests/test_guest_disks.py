"""guest-disks: orchestrator (disks.conf from pve-guest) and remote installer (homelab-ops#156).

The installer runs against a fake guest: by-id links and mount targets live under
`tmp_path`, and `blkid`/`lsblk`/`findmnt`/`mkfs`/`mount`/`systemctl` answer from
an in-memory disk model. Nothing here touches a real block device or /etc/fstab.
"""

from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from homelab.hosts import HostRegistry, default_registry
from homelab.modules import guest_disks
from homelab.modules.pve_guest.inventory import pve_nodes
from homelab.modules.pve_guest.spec import parse_guest
from homelab_install import files, systemd
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "guest-disks" / "scripts" / "install.py"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def vm_block(disks: dict) -> dict:
    return {
        "vmid": 200,
        "kind": "qemu",
        "home_node": "ace",
        "profile": "linux-vm",
        "cores": 2,
        "memory": 2048,
        "disks": disks,
        "net": {"net0": {"name": "nic0", "mac": "BC:24:11:00:00:01"}},
    }


OS = {"scsi0": {"role": "os", "size": "32G"}}


def registry_with(tmp_path: Path, guest: dict) -> HostRegistry:
    node = {"config": {"type": "pve", "hostname": "n", "user": "root", "sshkey": "infra"}}
    data = {"ace": node, "vm": guest}
    path = tmp_path / "hosts.conf"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return HostRegistry(path)


def guest(features: dict, host_type: str = "ubuntu") -> dict:
    config = {"type": host_type, "hostname": "g", "user": "root", "sshkey": "infra"}
    return {"config": config, "features": {"guest-disks": True, **features}}


def mounts_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: dict):
    registry = registry_with(tmp_path, entry)
    monkeypatch.setattr(guest_disks, "default_registry", lambda _root: registry)
    return guest_disks.disk_mounts(tmp_path, "vm")


def spec_of(disks: dict, kind: str = "qemu"):
    return parse_guest("vm", vm_block(disks), {"ace": False})


def test_helm_scratch_disk_renders_with_nofail() -> None:
    registry = default_registry(ROOT)
    spec = parse_guest("helm", registry.get("helm", "pve-guest"), pve_nodes(registry))

    mounts = guest_disks.plan_mounts(spec)

    assert mounts == (
        guest_disks.DiskMount("scsi1", "scratch", "/mnt/scratch", "defaults,noatime,nofail"),
    )
    assert guest_disks.render_disks_conf(mounts) == (
        "scsi1|scratch|/mnt/scratch|defaults,noatime,nofail\n"
    )


@pytest.mark.parametrize(
    ("role", "options"),
    [
        ("scratch", "defaults,noatime,nofail"),
        ("appdata", "defaults,noatime"),
        ("data", "defaults,noatime"),
    ],
)
def test_only_scratch_may_be_missing_at_boot(role: str, options: str) -> None:
    assert guest_disks.mount_options(role) == options


def test_mounts_are_ordered_parents_first_and_skip_the_os_disk() -> None:
    spec = spec_of(
        OS
        | {
            "scsi1": {"role": "data", "size": "8G", "mount": "/mnt/cache/appdata/big"},
            "scsi2": {"role": "appdata", "size": "8G"},
            "scsi3": {"role": "scratch", "size": "8G"},
        }
    )

    mounts = guest_disks.plan_mounts(spec)

    assert [(m.key, m.mount) for m in mounts] == [
        ("scsi2", "/mnt/cache/appdata"),
        ("scsi1", "/mnt/cache/appdata/big"),
        ("scsi3", "/mnt/scratch"),
    ]


def test_a_per_disk_mount_override_is_kept() -> None:
    spec = spec_of(OS | {"scsi1": {"role": "appdata", "size": "8G", "mount": "/home"}})

    assert guest_disks.plan_mounts(spec)[0].mount == "/home"


def test_a_non_scsi_disk_is_refused() -> None:
    spec = spec_of(OS | {"virtio1": {"role": "scratch", "size": "8G"}})

    with pytest.raises(ValueError, match="only mounts scsiN disks, not virtio1"):
        guest_disks.plan_mounts(spec)


def test_a_vm_with_nothing_to_mount_is_refused() -> None:
    with pytest.raises(ValueError, match="no pve-guest disk has a mount"):
        guest_disks.plan_mounts(spec_of(OS))


def test_an_lxc_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    block = {
        "vmid": 201,
        "kind": "lxc",
        "home_node": "ace",
        "profile": "plain-ct",
        "cores": 1,
        "memory": 512,
        "disks": {"rootfs": {"role": "os", "size": "8G"}, "mp0": {"role": "scratch"}},
        "net": {"net0": {"name": "nic0", "mac": "BC:24:11:00:00:02"}},
    }

    with pytest.raises(ValueError, match="is for VMs"):
        mounts_for(tmp_path, monkeypatch, guest({"pve-guest": block}))


def test_disk_mounts_reads_the_pve_guest_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = vm_block(OS | {"scsi1": {"role": "appdata", "size": "8G"}})

    mounts = mounts_for(tmp_path, monkeypatch, guest({"pve-guest": block}))

    assert mounts == (
        guest_disks.DiskMount("scsi1", "appdata", "/mnt/cache/appdata", "defaults,noatime"),
    )


def test_errors_name_the_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="^vm: guest-disks is set but no pve-guest disk"):
        mounts_for(tmp_path, monkeypatch, guest({"pve-guest": vm_block(OS)}))


def test_disks_conf_has_one_line_per_disk() -> None:
    mounts = (
        guest_disks.DiskMount("scsi1", "appdata", "/a", "o1"),
        guest_disks.DiskMount("scsi2", "scratch", "/b", "o2"),
    )

    assert guest_disks.render_disks_conf(mounts) == "scsi1|appdata|/a|o1\nscsi2|scratch|/b|o2\n"


def test_a_host_without_a_pve_guest_block_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="reads its disks from a pve-guest block"):
        mounts_for(tmp_path, monkeypatch, guest({}))


def test_a_non_ubuntu_host_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    block = vm_block(OS | {"scsi1": {"role": "scratch", "size": "8G"}})

    with pytest.raises(ValueError, match="only supports ubuntu hosts"):
        mounts_for(tmp_path, monkeypatch, guest({"pve-guest": block}, host_type="haos"))


def test_validate_checks_every_supported_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(guest_disks, "disk_mounts", lambda _root, host: seen.append(host))

    guest_disks.validate(ROOT, ["a", "b"])

    assert seen == ["a", "b"]
    with pytest.raises(ValueError, match="missing installer"):
        guest_disks.validate(tmp_path, ["a"])


class Staged:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, root, connection, remote_root, uploads, installer, *args, **kwargs):
        self.calls.append(
            {
                "root": root,
                "connection": connection,
                "remote_root": remote_root,
                "uploads": uploads,
                "installer": installer,
                "args": args,
            }
            | kwargs
        )


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Staged:
    block = vm_block(OS | {"scsi1": {"role": "scratch", "size": "8G"}})
    registry = registry_with(tmp_path, guest({"pve-guest": block}))
    monkeypatch.setattr(guest_disks, "default_registry", lambda _root: registry)
    monkeypatch.setattr(guest_disks, "connection_for_host", lambda root, host: ("conn", root, host))
    stage = Staged()
    monkeypatch.setattr(guest_disks, "stage_and_run_remote_installer", stage)
    monkeypatch.delenv("HOMELAB_OFFLINE", raising=False)
    return stage


@pytest.mark.parametrize(("dry_run", "plan_only"), [(True, "true"), (False, "false")])
def test_dry_run_runs_the_installer_in_plan_only_mode(
    tmp_path: Path, staged: Staged, dry_run: bool, plan_only: str
) -> None:
    guest_disks.deploy_host(tmp_path, "vm", dry_run=dry_run, force=False)

    (call,) = staged.calls
    assert call["root"] == tmp_path
    assert call["connection"] == ("conn", tmp_path, "vm")
    assert call["remote_subdirs"] == ("build", "src")
    assert call["env"] == {"FORCE_UPDATE": "false", "PLAN_ONLY": plan_only}
    assert call["remote_root"] == "/tmp/homelab-guest-disks"
    assert call["installer"] == "scripts/install.py"
    assert call["args"] == ("vm",)
    assert call["require_root"] is True
    assert call["interpreter"] == "python3"
    build = tmp_path / "guest-disks" / "build" / "vm"
    assert call["uploads"] == [
        (build, "/tmp/homelab-guest-disks/build/vm"),
        (tmp_path / "guest-disks" / "scripts", "/tmp/homelab-guest-disks/scripts"),
    ]
    assert (
        build / "disks.conf"
    ).read_text() == "scsi1|scratch|/mnt/scratch|defaults,noatime,nofail\n"


def test_force_reaches_the_installer(tmp_path: Path, staged: Staged) -> None:
    guest_disks.deploy_host(tmp_path, "vm", dry_run=False, force=True)

    assert staged.calls[0]["env"]["FORCE_UPDATE"] == "true"


def test_an_offline_dry_run_renders_but_never_stages(
    tmp_path: Path,
    staged: Staged,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOMELAB_OFFLINE", "1")

    guest_disks.deploy_host(tmp_path, "vm", dry_run=True, force=False)

    assert staged.calls == []
    assert (tmp_path / "guest-disks" / "build" / "vm" / "disks.conf").is_file()
    out = capsys.readouterr().out
    assert "scsi1 (scratch) -> /mnt/scratch [defaults,noatime,nofail]" in out
    assert "    [?] live disk plan skipped (offline validation)\n" in out


def test_deploy_wires_validate_and_deploy_host_for_the_requested_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def fake_run(root, requested_host, feature, session, deploy_host, validate):
        seen.update(root=root, requested=requested_host, feature=feature, session=session)
        validate(["a", "b"], ["a"])
        deploy_host("a")
        return 7

    monkeypatch.setattr(guest_disks, "run_module_deploy", fake_run)
    monkeypatch.setattr(
        guest_disks, "validate", lambda root, hosts: seen.update(valid=(root, hosts))
    )
    monkeypatch.setattr(
        guest_disks,
        "deploy_host",
        lambda root, host, dry_run, force: seen.update(host=(root, host, dry_run, force)),
    )

    assert guest_disks.deploy(tmp_path, "a", True, True, "session") == 7
    assert seen == {
        "root": tmp_path,
        "requested": "a",
        "feature": "guest-disks",
        "session": "session",
        "valid": (tmp_path, ["a", "b"]),
        "host": (tmp_path, "a", True, True),
    }


# ---------------------------------------------------------------------------
# Installer: pure parts
# ---------------------------------------------------------------------------


def load_installer():
    spec = importlib.util.spec_from_file_location("guest_disks_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inst = load_installer()
SCRATCH = inst.Wanted("scsi1", "scratch", "/mnt/scratch", "defaults,noatime,nofail")
BY_ID = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1"
UUID = "8de3d7ba-3a81-4e6c-a11f-a02b2f3134f2"


def disk(**overrides):
    values = {"by_id": BY_ID, "device": "/dev/sdb", "fstype": "ext4", "uuid": UUID}
    return inst.Probe(**(values | overrides))


def managed(source: str = f"UUID={UUID}", target: str = "/mnt/scratch", key: str = "scsi1"):
    return inst.FstabEntry(source, target, key)


def foreign(source: str, target: str):
    return inst.FstabEntry(source, target, None)


def test_disks_conf_is_parsed_and_malformed_lines_are_refused() -> None:
    assert inst.parse_disks("\nscsi1|scratch|/mnt/scratch|defaults\n") == [
        inst.Wanted("scsi1", "scratch", "/mnt/scratch", "defaults")
    ]
    for bad in ("scsi1|scratch|/mnt/scratch", "scsi1||/mnt/scratch|defaults"):
        with pytest.raises(InstallError, match="malformed"):
            inst.parse_disks(bad)
    for mount in ("mnt/scratch", "/"):
        with pytest.raises(InstallError, match="absolute path below /"):
            inst.parse_disks(f"scsi1|scratch|{mount}|defaults")
    for mount in ("/mnt/my scratch", "/mnt/tab\there"):
        with pytest.raises(InstallError, match="scsi1 mount path must not contain whitespace"):
            inst.parse_disks(f"scsi1|scratch|{mount}|defaults")
    with pytest.raises(InstallError, match="lists no disks"):
        inst.parse_disks("\n")


def test_fstab_entries_know_which_marker_owns_them() -> None:
    text = (
        "# a comment\n"
        "PARTUUID=x / ext4 rw 0 1\n"
        f"{inst.MARKER_PREFIX}scsi1\n"
        f"UUID={UUID} /mnt/scratch ext4 defaults 0 2\n"
        "\n"
        "/dev/sdc /mnt/other ext4 defaults 0 2\n"
        "garbage\n"
    )

    assert inst.parse_fstab(text) == [
        foreign("PARTUUID=x", "/"),
        managed(),
        foreign("/dev/sdc", "/mnt/other"),
    ]


def test_a_marker_owns_only_the_entry_after_it() -> None:
    text = f"{inst.MARKER_PREFIX}scsi1\n# note\nUUID=a /a ext4 d 0 2\nUUID=b /b ext4 d 0 2\n"

    assert inst.parse_fstab(text) == [managed("UUID=a", "/a"), foreign("UUID=b", "/b")]


def test_a_blank_disk_is_formatted_listed_and_mounted() -> None:
    plan = inst.decide(SCRATCH, disk(fstype=None, uuid=None), [])

    assert (plan.mkfs, plan.write_fstab, plan.mount, plan.refusals) == (True, True, True, [])
    assert plan.summary() == (
        "scsi1 (scratch) -> /mnt/scratch: mkfs ext4, write fstab entry, mount"
    )


def test_a_converged_disk_is_left_in_place() -> None:
    plan = inst.decide(SCRATCH, disk(mounted_at=("/mnt/scratch",)), [managed()])

    assert (plan.mkfs, plan.write_fstab, plan.mount, plan.refusals) == (False, False, False, [])
    assert plan.summary() == "scsi1 (scratch) -> /mnt/scratch: in place"


def test_a_formatted_unmounted_disk_is_listed_and_mounted_without_mkfs() -> None:
    plan = inst.decide(SCRATCH, disk(target_state="empty"), [])

    assert (plan.mkfs, plan.write_fstab, plan.mount, plan.refusals) == (False, True, True, [])


@pytest.mark.parametrize(
    "entry",
    [managed(source="UUID=old"), managed(target="/mnt/old")],
    ids=["new-uuid", "new-path"],
)
def test_a_stale_managed_entry_is_rewritten(entry) -> None:
    plan = inst.decide(SCRATCH, disk(mounted_at=("/mnt/scratch",)), [entry])

    assert plan.write_fstab is True
    assert plan.refusals == []


@pytest.mark.parametrize(
    ("probe", "fstab", "reason"),
    [
        (disk(device=None), [], f"no disk at {BY_ID}"),
        (disk(fstype=None, uuid=None, partitioned=True), [], "has a partition table"),
        (disk(fstype="xfs"), [], "holds xfs, not ext4"),
        # helm today: the scratch disk is a hand-made /mnt/vmdata mount (homelab-ops#158).
        (disk(), [foreign(f"UUID={UUID}", "/mnt/vmdata")], "fstab already lists this disk"),
        (disk(), [foreign("/dev/sdb", "/mnt/vmdata")], "fstab already lists this disk"),
        (disk(), [foreign(BY_ID, "/mnt/vmdata")], "fstab already lists this disk"),
        (
            disk(),
            [foreign(f"/dev/disk/by-uuid/{UUID}", "/x")],
            "fstab already lists this disk",
        ),
        (disk(), [foreign("UUID=other", "/mnt/scratch")], "fstab already mounts UUID=other"),
        (disk(), [managed(source="UUID=other", key="scsi9")], "fstab already mounts"),
        (
            disk(mounted_at=("/mnt/vmdata", "/mnt/cache/appdata/victoria-metrics")),
            [],
            "already mounted at /mnt/vmdata, /mnt/cache/appdata/victoria-metrics",
        ),
        (disk(target_source="/dev/sdc"), [], "/dev/sdc is already mounted at /mnt/scratch"),
        (disk(target_state="busy"), [], "/mnt/scratch is not empty"),
        (disk(target_state="not-a-dir"), [], "exists and is not a directory"),
    ],
)
def test_refusals(probe, fstab, reason: str) -> None:
    plan = inst.decide(SCRATCH, probe, fstab)

    assert any(reason in refusal for refusal in plan.refusals), plan.refusals
    assert "REFUSED" in plan.summary()


def test_a_partitioned_disk_is_never_formatted() -> None:
    plan = inst.decide(SCRATCH, disk(fstype=None, uuid=None, partitioned=True), [])

    assert plan.mkfs is False


def test_mounted_at_the_target_and_elsewhere_still_refuses() -> None:
    plan = inst.decide(SCRATCH, disk(mounted_at=("/mnt/scratch", "/other")), [managed()])

    assert plan.refusals == ["already mounted at /other; unmount it first"]
    assert plan.mount is False


def test_render_fstab_replaces_a_marked_entry_and_keeps_every_other_line() -> None:
    text = (
        "PARTUUID=x / ext4 rw 0 1\n"
        f"{inst.MARKER_PREFIX}scsi1\n"
        "\n"
        "UUID=old /mnt/scratch ext4 defaults 0 2\n"
        "/dev/sdc /mnt/other ext4 defaults 0 2\n"
    )

    out = inst.render_fstab(text, {"scsi1": "NEW", "scsi2": "TWO"})

    assert out == (
        "PARTUUID=x / ext4 rw 0 1\n"
        f"{inst.MARKER_PREFIX}scsi1\n"
        "NEW\n"
        "\n"
        "/dev/sdc /mnt/other ext4 defaults 0 2\n"
        f"{inst.MARKER_PREFIX}scsi2\n"
        "TWO\n"
    )


def test_render_fstab_leaves_other_marked_entries_alone() -> None:
    text = f"{inst.MARKER_PREFIX}scsi3\nUUID=c /c ext4 d 0 2\n"

    assert inst.render_fstab(text, {"scsi1": "NEW"}) == (f"{text}{inst.MARKER_PREFIX}scsi1\nNEW\n")


def test_fstab_line_shape() -> None:
    assert inst.fstab_line(SCRATCH, UUID) == (
        f"UUID={UUID} /mnt/scratch ext4 defaults,noatime,nofail 0 2"
    )


# ---------------------------------------------------------------------------
# Installer: against a fake guest
# ---------------------------------------------------------------------------


class Guest:
    """A VM with role disks: blkid/lsblk/findmnt/mkfs/mount answer from `disks`."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.installer = load_installer()
        self.root = tmp_path / "guest"
        self.by_id = self.root / "dev/disk/by-id"
        self.by_id.mkdir(parents=True)
        self.fstab = self.root / "etc/fstab"
        self.fstab.parent.mkdir(parents=True)
        self.fstab.write_text("PARTUUID=x / ext4 rw 0 1\n", encoding="utf-8")
        self.build = tmp_path / "stage/build/vm"
        self.build.mkdir(parents=True)
        monkeypatch.setattr(self.installer, "BY_ID_DIR", str(self.by_id))
        monkeypatch.setattr(self.installer, "FSTAB", str(self.fstab))
        self.dropin = self.root / "etc/systemd/system/docker.service.d/10-guest-disks.conf"
        monkeypatch.setattr(self.installer, "DOCKER_DROPIN", str(self.dropin))
        monkeypatch.setattr(files, "BACKUP_KEEP_COUNT", 3)
        # device -> {"TYPE":..., "UUID":...}; device -> set of mount targets
        self.blk: dict[str, dict[str, str]] = {}
        self.mounts: dict[str, set[str]] = {}
        self.calls: list[list[str]] = []
        self.mkfs_uuid = "11111111-2222-3333-4444-555555555555"
        for target in (self.installer, systemd):
            monkeypatch.setattr(target, "_run", self.run)

    def mount_path(self, name: str) -> str:
        return str(self.root / name)

    def add_disk(self, key: str, fstype: str | None = None, uuid: str | None = None) -> str:
        device = str(self.root / f"dev/{key}")
        Path(device).touch()
        (self.by_id / f"scsi-0QEMU_QEMU_HARDDISK_drive-{key}").symlink_to(device)
        self.blk[device] = {k: v for k, v in (("TYPE", fstype), ("UUID", uuid)) if v}
        self.mounts[device] = set()
        return device

    def want(self, *lines: str) -> None:
        (self.build / "disks.conf").write_text("".join(f"{line}\n" for line in lines))

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        code, out = 0, ""
        if argv[0] == "blkid":
            info = self.blk.get(argv[-1], {})
            code, out = (0, "".join(f"{k}={v}\n" for k, v in info.items())) if info else (2, "")
        elif argv[0] == "lsblk":
            out = "disk\n"
        elif argv[0] == "findmnt" and "--source" in argv:
            targets = sorted(self.mounts.get(argv[-1], ()))
            code, out = (0, "".join(f"{t}\n" for t in targets)) if targets else (1, "")
        elif argv[0] == "findmnt":
            sources = [dev for dev, targets in self.mounts.items() if argv[-1] in targets]
            code, out = (0, f"{sources[0]}\n") if sources else (1, "")
        elif argv[0] == "mkfs.ext4":
            self.blk[argv[-1]] = {"TYPE": "ext4", "UUID": self.mkfs_uuid}
        elif argv[0] == "mount":
            for line in self.fstab.read_text().splitlines():
                fields = line.split()
                if len(fields) > 1 and fields[1] == argv[1]:
                    uuid = fields[0].removeprefix("UUID=")
                    dev = next(d for d, info in self.blk.items() if info.get("UUID") == uuid)
                    self.mounts[dev].add(argv[1])
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    def ran(self, name: str) -> list[list[str]]:
        return [call for call in self.calls if call[0] == name]

    def install(self, plan_only: bool = False) -> None:
        ctx = InstallContext(
            host="vm",
            script_dir=self.build.parent.parent,
            build_dir=self.build,
            env={},
            deploy_env={"PLAN_ONLY": "true" if plan_only else "false"},
            file_map={},
            force_update=False,
        )
        self.installer.install(ctx)


@pytest.fixture
def vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Guest:
    return Guest(tmp_path, monkeypatch)


def test_a_blank_disk_is_formatted_listed_and_mounted_on_a_live_run(vm: Guest) -> None:
    device = vm.add_disk("scsi1")
    mount = vm.mount_path("mnt/scratch")
    vm.want(f"scsi1|scratch|{mount}|defaults,noatime,nofail")

    vm.install()

    assert vm.ran("mkfs.ext4") == [["mkfs.ext4", "-q", "-F", device]]
    assert vm.fstab.read_text() == (
        "PARTUUID=x / ext4 rw 0 1\n"
        f"{vm.installer.MARKER_PREFIX}scsi1\n"
        f"UUID={vm.mkfs_uuid} {mount} ext4 defaults,noatime,nofail 0 2\n"
    )
    assert vm.ran("systemctl") == [["systemctl", "daemon-reload"]] * 2  # fstab, then drop-in
    assert vm.ran("mount") == [["mount", mount]]
    assert vm.mounts[device] == {mount}
    assert list(vm.fstab.parent.glob("fstab.bak.*"))
    assert vm.dropin.read_text() == (
        "# Managed by homelab guest-disks (homelab-ops#174): Docker starts only once\n"
        "# every role disk is mounted, so no stack writes into an empty mount point.\n"
        "[Unit]\n"
        f"RequiresMountsFor={mount}\n"
    )
    assert vm.dropin.stat().st_mode & 0o777 == 0o644


def test_docker_waits_for_every_managed_mount_in_mount_order() -> None:
    wanted = [
        inst.Wanted("scsi2", "appdata", "/mnt/cache/appdata", "d"),
        inst.Wanted("scsi1", "scratch", "/mnt/scratch", "d,nofail"),
    ]
    assert inst.docker_dropin(wanted).splitlines()[-2:] == [
        "[Unit]",
        "RequiresMountsFor=/mnt/cache/appdata /mnt/scratch",
    ]


def converge(vm: Guest, mount: str) -> str:
    device = vm.add_disk("scsi1", "ext4", UUID)
    vm.mounts[device].add(mount)
    vm.fstab.write_text(
        f"{vm.installer.MARKER_PREFIX}scsi1\nUUID={UUID} {mount} ext4 defaults 0 2\n"
    )
    vm.want(f"scsi1|scratch|{mount}|defaults")
    return vm.fstab.read_text()


def test_a_converged_guest_is_not_touched(vm: Guest) -> None:
    mount = vm.mount_path("mnt/scratch")
    before = converge(vm, mount)
    wanted = vm.installer.parse_disks((vm.build / "disks.conf").read_text())
    vm.dropin.parent.mkdir(parents=True)
    vm.dropin.write_text(vm.installer.docker_dropin(wanted))

    vm.install()

    assert vm.fstab.read_text() == before
    for tool in ("mkfs.ext4", "mount", "systemctl"):
        assert vm.ran(tool) == []
    assert not list(vm.fstab.parent.glob("fstab.bak.*"))


@pytest.mark.parametrize("old", [None, "[Unit]\nRequiresMountsFor=/mnt/gone\n"])
def test_mounted_disks_with_a_missing_or_stale_dropin_only_rewrite_it(
    vm: Guest, old: str | None
) -> None:
    """helm-style upgrade: disks already converged before the drop-in existed."""
    mount = vm.mount_path("mnt/scratch")
    before = converge(vm, mount)
    if old is not None:
        vm.dropin.parent.mkdir(parents=True)
        vm.dropin.write_text(old)

    vm.install()

    assert vm.fstab.read_text() == before
    assert vm.ran("mkfs.ext4") == vm.ran("mount") == []
    assert vm.ran("systemctl") == [["systemctl", "daemon-reload"]]
    assert vm.dropin.read_text().endswith(f"RequiresMountsFor={mount}\n")
    # Never restarted: it applies from Docker's next start.
    assert not any("docker" in " ".join(call) for call in vm.calls)


def test_plan_only_writes_nothing(vm: Guest, capsys: pytest.CaptureFixture[str]) -> None:
    device = vm.add_disk("scsi1")
    mount = vm.mount_path("mnt/scratch")
    vm.want(f"scsi1|scratch|{mount}|defaults")
    before = vm.fstab.read_text()

    vm.install(plan_only=True)

    assert vm.fstab.read_text() == before
    assert vm.ran("mkfs.ext4") == vm.ran("mount") == []
    assert vm.mounts[device] == set()
    assert not Path(mount).exists()
    assert not vm.dropin.exists()
    out = capsys.readouterr().out
    assert "mkfs ext4, write fstab entry, mount" in out
    assert "docker.service drop-in: write" in out
    assert "PLAN_ONLY: nothing written" in out


@pytest.mark.parametrize("plan_only", [True, False])
def test_one_refused_disk_stops_every_disk(vm: Guest, plan_only: bool) -> None:
    """helm today: scsi1 is a hand-made /mnt/vmdata mount; a blank scsi2 must not be
    formatted on the strength of the other disk's plan being fine."""
    vm.add_disk("scsi1")
    held = vm.add_disk("scsi2", "ext4", UUID)
    vm.mounts[held].add("/mnt/vmdata")
    vm.fstab.write_text(f"UUID={UUID} /mnt/vmdata ext4 defaults 0 2\n")
    before = vm.fstab.read_text()
    vm.want(
        f"scsi1|appdata|{vm.mount_path('mnt/cache/appdata')}|defaults",
        f"scsi2|scratch|{vm.mount_path('mnt/scratch')}|defaults,nofail",
    )

    with pytest.raises(InstallError, match="1 disk"):
        vm.install(plan_only=plan_only)

    assert vm.fstab.read_text() == before
    assert vm.ran("mkfs.ext4") == vm.ran("mount") == []
    assert not vm.dropin.exists()


def test_a_non_empty_target_is_refused(vm: Guest) -> None:
    vm.add_disk("scsi1", "ext4", UUID)
    home = Path(vm.mount_path("home"))
    (home / "freender").mkdir(parents=True)
    vm.want(f"scsi1|appdata|{home}|defaults")

    with pytest.raises(InstallError):
        vm.install()

    assert vm.ran("mount") == []


def test_a_mount_that_does_not_take_fails_the_deploy(
    vm: Guest, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm.add_disk("scsi1", "ext4", UUID)
    vm.want(f"scsi1|scratch|{vm.mount_path('mnt/scratch')}|defaults")
    real = vm.run
    monkeypatch.setattr(
        vm.installer,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "mount"
        else real(argv, **kw),
    )

    with pytest.raises(InstallError, match="not mounted after deploy"):
        vm.install()
    assert not vm.dropin.exists()


def test_mkfs_that_leaves_no_uuid_fails_before_fstab(vm: Guest) -> None:
    device = vm.add_disk("scsi1")
    vm.mkfs_uuid = ""
    vm.want(f"scsi1|scratch|{vm.mount_path('mnt/scratch')}|defaults")
    before = vm.fstab.read_text()

    with pytest.raises(InstallError, match=f"{device} has no UUID after mkfs"):
        vm.install()

    assert vm.fstab.read_text() == before


def test_a_stale_managed_entry_warns_and_stays(
    vm: Guest, capsys: pytest.CaptureFixture[str]
) -> None:
    mount = vm.mount_path("mnt/scratch")
    device = vm.add_disk("scsi1", "ext4", UUID)
    vm.mounts[device].add(mount)
    stale = f"{vm.installer.MARKER_PREFIX}scsi7\nUUID=gone /mnt/gone ext4 defaults 0 2\n"
    vm.fstab.write_text(
        f"{stale}{vm.installer.MARKER_PREFIX}scsi1\nUUID={UUID} {mount} ext4 defaults 0 2\n"
    )
    vm.want(f"scsi1|scratch|{mount}|defaults")

    vm.install()

    assert vm.fstab.read_text().startswith(stale)
    assert "guest-disks entry for scsi7, no longer in spec" in capsys.readouterr().out


def test_probe_reads_partitions_from_blkid_or_lsblk(
    vm: Guest, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = vm.add_disk("scsi1")
    vm.blk[device] = {"PTTYPE": "gpt"}
    assert vm.installer.probe(vm.installer.Wanted("scsi1", "s", "/x", "d")).partitioned

    vm.blk[device] = {}
    real = vm.run
    monkeypatch.setattr(
        vm.installer,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "disk\npart\n", "")
        if argv[0] == "lsblk"
        else real(argv, **kw),
    )
    assert vm.installer.probe(vm.installer.Wanted("scsi1", "s", "/x", "d")).partitioned


def test_target_state(vm: Guest) -> None:
    root = Path(vm.mount_path("t"))
    assert vm.installer.target_state(str(root)) == "missing"
    root.mkdir()
    assert vm.installer.target_state(str(root)) == "empty"
    (root / "f").touch()
    assert vm.installer.target_state(str(root)) == "busy"
    assert vm.installer.target_state(str(root / "f")) == "not-a-dir"


def test_the_real_orchestrator_render_parses_on_the_guest() -> None:
    registry = default_registry(ROOT)
    spec = parse_guest(
        "helm", copy.deepcopy(registry.get("helm", "pve-guest")), pve_nodes(registry)
    )
    text = guest_disks.render_disks_conf(guest_disks.plan_mounts(spec))

    assert inst.parse_disks(text) == [
        inst.Wanted("scsi1", "scratch", "/mnt/scratch", "defaults,noatime,nofail")
    ]
