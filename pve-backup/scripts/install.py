#!/usr/bin/env python3
"""Remote installer for the pve-backup module (freender/homelab-ops#30).

Three independent steps, each switched on by the presence of its rendered plan:

* `storage-plan.conf` -- PBS storage definitions in `/etc/pve/storage.cfg`, their
  passwords, and client-side encryption.
* `restore-plan.conf` -- on a rebuilt standalone node, fetch `/etc/pve` from PBS and
  put back the notification config and the listed LXC configs.
* `jobs-plan.conf` -- vzdump jobs in `/etc/pve/jobs.cfg`.

Replaces `install.sh` and its three sub-installers (815 lines of bash). The plans
are rendered exactly as before; they are now parsed rather than `source`d.

Behaviour changes from the bash:

* **A converged storage or job is no longer rewritten.** The bash ran `pvesm set`
  with the password on every storage and up to five `pvesh set` calls per job on
  every deploy, so `storage.cfg`, `jobs.cfg` and each `.pw` file carried the
  last deploy's mtime on both hosts. Each object is now read back and compared,
  the password against its `.pw` file, and only a difference writes -- once.
* **`set` takes away as well as adds** on the properties this module owns: an
  emptied `namespace`, a stale `exclude-path` list, and `all` left on a job that
  now names a `vmid`. This is the sixth "a step did less than it looked" finding.
* **Every refusal comes before any write:** a malformed plan, a missing password,
  a missing staged keyfile, a job with both `vmid` and `exclude`, an invalid VMID,
  a missing `pvesm`/`pvesh`/`proxmox-backup-client`.
* **Every staged secret is destroyed on every exit.** The bash's EXIT trap shredded
  the storage tokens and keyfile but never the per-destination `pbs-<n>.env`
  credentials the restore step reads; those stayed in `/tmp` until the next deploy.
  The decrypted `/tmp/pve-config-restore` copy of `/etc/pve` is also removed once
  it has been copied to its root-only home.
* **The snapshot is chosen from JSON**, the newest one that actually holds the
  archive, not by splitting the client's box-drawn table on `│`. A listing that
  fails is reported with its error; the bash sent stderr to /dev/null, so a bad
  credential read as "no snapshot found".
* **An autostarted LXC that is already running is not started again.** Under
  `--force` the bash ran `pct start` on running containers and failed the deploy.
* **Dead paths are gone:** the `/run/homelab-pve-backup/backup-state.env` file
  nothing read, and the `/etc/homelab/pbs-tokens.env` fallback -- the orchestrator
  has staged every password since the 1Password move, so the fallback could only
  ever supply a stale one.

Kept deliberately: an existing `encryption-key` is never replaced (that would
orphan every encrypted snapshot), a storage that cannot be reached yet is a warning
not a failure, and a storage whose job storage is inactive skips that job.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from homelab_install import log, run
from homelab_install.context import InstallContext
from homelab_install.errors import InstallError

# Indirection points for tests, same pattern as `homelab_install.systemd._run`.
_run = subprocess.run
_which = shutil.which

STORAGE_PLAN = "storage-plan.conf"
RESTORE_PLAN = "restore-plan.conf"
JOBS_PLAN = "jobs-plan.conf"
STAGED_TOKENS = "pbs-tokens.env"
STAGED_KEYFILE = "pbs-encryption.key"

PERSISTENT_KEYFILE = "/etc/homelab/pbs-encryption.key"
STORAGE_CFG = "/etc/pve/storage.cfg"
STORAGE_PRIV = "/etc/pve/priv/storage"
PVE_DIR = "/etc/pve"
RESTORE_ROOT = "/tmp/pve-config-restore"
RESTORE_OUTPUT = "/var/lib/homelab/pve-config-restore"

JOBS_ROOT = "/cluster/backup"
PRUNE_KEEP_ALL = "keep-all=1"
STORAGE_FIELDS = (
    "NAME",
    "SERVER",
    "DATASTORE",
    "NAMESPACE",
    "USERNAME",
    "FINGERPRINT",
    "PASSWORD_VAR",
    "ENCRYPTION",
)
JOB_FIELDS = (
    "SCHEDULE",
    "STORAGE",
    "VMID",
    "EXCLUDE",
    "COMPRESS",
    "MODE",
    "NOTES_TEMPLATE",
    "NOTIFICATION_MODE",
    "PRUNE_BACKUPS",
    "ENABLED",
    "FLEECING",
    "EXCLUDE_PATH_COUNT",
)
RESTORE_FIELDS = (
    "BACKUP_ID",
    "ARCHIVE_NAME",
    "ENCRYPT",
    "KEYFILE",
    "RESTORE_LXC_CONFIGS_ENABLED",
    "RESTORE_LXC_AUTOSTART",
    "RESTORE_LXC_CONFIG_COUNT",
    "DESTINATION_COUNT",
)
# Property strings PVE returns parsed into an object, keyed by their default key.
PROPERTY_STRINGS = {"prune-backups": None, "fleecing": "enabled"}
VMID = re.compile(r"[1-9][0-9]{0,8}")
TRANSIENT = re.compile(
    r"Can't connect|Connection timed out|No route to host|Network is unreachable|"
    r"Connection refused|Temporary failure|Name or service not known|could not resolve",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Storage:
    name: str
    server: str
    datastore: str
    namespace: str
    username: str
    fingerprint: str
    password: str
    encryption: bool


@dataclass(frozen=True)
class Job:
    index: int
    options: dict[str, object]  # pvesh options this module owns, in call order
    storage: str
    vmid: str
    exclude: str


@dataclass(frozen=True)
class Restore:
    backup_id: str
    archive: str
    namespace: str
    keyfile: str | None
    lxc_enabled: bool
    autostart: bool
    vmids: tuple[str, ...]
    repositories: tuple[str, ...]


# --- reading ------------------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    """Parse a `KEY=value` file the way bash `source` read these, without running it.

    One parser for all three shapes staged here: the orchestrator's single-quoted
    plans, its commented tokens file, and `op inject`'s double-quoted credentials.
    Values are never echoed in an error -- two of the three hold passwords.
    """
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise InstallError(f"malformed line in {path}:{number} (expected KEY=value)")
        try:
            tokens = shlex.split(value)
        except ValueError as exc:
            raise InstallError(f"unparseable value in {path}:{number}") from exc
        values[key.strip()] = tokens[0] if tokens else ""
    return values


def field(values: dict[str, str], key: str, path: Path) -> str:
    if key not in values:
        raise InstallError(f"{path.name} is missing {key}; refusing a partial plan")
    return values[key]


def count(values: dict[str, str], key: str, path: Path) -> int:
    raw = field(values, key, path)
    if not raw.isdigit():
        raise InstallError(f"{key} in {path.name} must be a non-negative integer, got {raw!r}")
    return int(raw)


def boolean(values: dict[str, str], key: str, path: Path) -> bool:
    raw = field(values, key, path)
    if raw not in ("true", "false"):
        raise InstallError(f"{key} in {path.name} must be true or false, got {raw!r}")
    return raw == "true"


def require_command(name: str) -> None:
    if not _which(name):
        raise InstallError(f"{name} command not found")


def command(args: list[str], *, secret: bool = False, env: dict[str, str] | None = None) -> str:
    result = _run(args, check=False, capture_output=True, text=True, env=env)
    if result.returncode == 0:
        return result.stdout
    detail = (
        "(output withheld: the call carried a password)"
        if secret
        else ((result.stderr or result.stdout or "").strip())
    )
    raise InstallError(f"{' '.join(args[:3])} failed: {detail}")


# --- plans --------------------------------------------------------------------


def read_storages(ctx: InstallContext) -> list[Storage]:
    path = ctx.build_dir / STORAGE_PLAN
    plan = read_env(path)
    tokens_path = ctx.build_dir / STAGED_TOKENS
    tokens = read_env(tokens_path) if tokens_path.is_file() else {}
    storages = []
    for index in range(count(plan, "STORAGE_COUNT", path)):
        values = {key: field(plan, f"STORAGE_{index}_{key}", path) for key in STORAGE_FIELDS}
        for key in ("NAME", "SERVER", "DATASTORE", "USERNAME", "FINGERPRINT", "PASSWORD_VAR"):
            if not values[key]:
                raise InstallError(f"STORAGE_{index}_{key} is empty in {path.name}")
        password = tokens.get(values["PASSWORD_VAR"], "")
        if not password:
            raise InstallError(
                f"missing {values['PASSWORD_VAR']} in staged pve-backup tokens; "
                f"run ./deploy pve-backup {ctx.host} from riven"
            )
        storages.append(
            Storage(
                name=values["NAME"],
                server=values["SERVER"],
                datastore=values["DATASTORE"],
                namespace=values["NAMESPACE"],
                username=values["USERNAME"],
                fingerprint=values["FINGERPRINT"],
                password=password,
                encryption=boolean(plan, f"STORAGE_{index}_ENCRYPTION", path),
            )
        )
    if (
        any(storage.encryption for storage in storages)
        and not (ctx.build_dir / STAGED_KEYFILE).is_file()
    ):
        raise InstallError(
            "a storage requests encryption but the keyfile was not staged; "
            f"run ./deploy pve-backup {ctx.host} from riven"
        )
    return storages


def job_options(values: dict[str, str], paths: list[str]) -> dict[str, object]:
    options: dict[str, object] = {
        "schedule": values["SCHEDULE"],
        "storage": values["STORAGE"],
        "compress": values["COMPRESS"],
        "mode": values["MODE"],
        "notes-template": values["NOTES_TEMPLATE"],
        "notification-mode": values["NOTIFICATION_MODE"],
        "prune-backups": values["PRUNE_BACKUPS"],
        "enabled": values["ENABLED"],
        "fleecing": values["FLEECING"],
    }
    if values["VMID"]:
        options["vmid"] = values["VMID"]
    else:
        options["all"] = "1"
        if values["EXCLUDE"]:
            options["exclude"] = values["EXCLUDE"]
    if paths:
        options["exclude-path"] = paths
    return options


def read_jobs(ctx: InstallContext) -> list[Job]:
    path = ctx.build_dir / JOBS_PLAN
    plan = read_env(path)
    jobs = []
    for index in range(count(plan, "JOB_COUNT", path)):
        values = {key: field(plan, f"JOB_{index}_{key}", path) for key in JOB_FIELDS}
        if not values["SCHEDULE"] or not values["STORAGE"]:
            raise InstallError(f"job {index} is missing its schedule or storage")
        if values["VMID"] and values["EXCLUDE"]:
            raise InstallError(f"job {index} cannot set both vmid and exclude")
        paths = [
            field(plan, f"JOB_{index}_EXCLUDE_PATH_{number}", path)
            for number in range(count(plan, f"JOB_{index}_EXCLUDE_PATH_COUNT", path))
        ]
        jobs.append(
            Job(
                index,
                job_options(values, paths),
                values["STORAGE"],
                values["VMID"],
                values["EXCLUDE"],
            )
        )
    return jobs


def read_restore(ctx: InstallContext) -> Restore:
    path = ctx.build_dir / RESTORE_PLAN
    plan = read_env(path)
    values = {key: field(plan, key, path) for key in RESTORE_FIELDS}
    if not values["BACKUP_ID"] or not values["ARCHIVE_NAME"]:
        raise InstallError("restore plan is missing BACKUP_ID or ARCHIVE_NAME")
    vmids = tuple(
        field(plan, f"RESTORE_LXC_CONFIG_{index}_VMID", path)
        for index in range(count(plan, "RESTORE_LXC_CONFIG_COUNT", path))
    )
    invalid = [vmid for vmid in vmids if not VMID.fullmatch(vmid)]
    if invalid:
        raise InstallError(f"invalid LXC VMID in restore plan: {', '.join(invalid)}")
    lxc_enabled = boolean(plan, "RESTORE_LXC_CONFIGS_ENABLED", path)
    if lxc_enabled and not vmids:
        raise InstallError("LXC config restore is enabled but lists no VMIDs")
    repositories = tuple(
        field(plan, f"DESTINATION_{index}_REPOSITORY", path)
        for index in range(count(plan, "DESTINATION_COUNT", path))
    )
    if not any(repositories):
        raise InstallError("restore plan has no PBS destinations")
    return Restore(
        backup_id=values["BACKUP_ID"],
        archive=values["ARCHIVE_NAME"],
        namespace=field(plan, "NAMESPACE", path),
        keyfile=values["KEYFILE"] if boolean(plan, "ENCRYPT", path) else None,
        lxc_enabled=lxc_enabled,
        autostart=boolean(plan, "RESTORE_LXC_AUTOSTART", path),
        vmids=vmids,
        repositories=repositories,
    )


def check_restore_keyfile(ctx: InstallContext, restore: Restore) -> None:
    """The keyfile may not be on disk yet on a rebuilt node: the storage step
    installs it from the staged copy first, so a staged key counts."""
    if restore.keyfile is None or Path(restore.keyfile).is_file():
        return
    if restore.keyfile == PERSISTENT_KEYFILE and (ctx.build_dir / STAGED_KEYFILE).is_file():
        return
    raise InstallError(f"restore plan marks encryption but keyfile missing: {restore.keyfile}")


# --- secrets ------------------------------------------------------------------


def destroy(path: Path) -> None:
    if not path.exists():
        return
    if _which("shred"):
        _run(["shred", "-u", "-n", "1", str(path)], check=False)
    path.unlink(missing_ok=True)


def destroy_staged_secrets(ctx: InstallContext) -> None:
    for path in [
        ctx.build_dir / STAGED_TOKENS,
        ctx.build_dir / STAGED_KEYFILE,
        *sorted(ctx.build_dir.glob("pbs-*.env")),
    ]:
        destroy(path)


def install_keyfile(ctx: InstallContext) -> None:
    """Keep the shared key where encrypted host-archive restores read it, so a
    direct pve-backup deploy on a rebuilt node does not depend on
    pbs-client-backup having run first. Created at 600, never widened first."""
    staged = ctx.build_dir / STAGED_KEYFILE
    if not staged.is_file():
        return
    dest = Path(PERSISTENT_KEYFILE)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.parent.chmod(0o700)
    key = staged.read_bytes()
    if dest.is_file() and dest.read_bytes() == key and not ctx.force_update:
        dest.chmod(0o600)
        return
    descriptor = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    dest.chmod(0o600)
    log.sub(f"Installed PBS encryption keyfile at {PERSISTENT_KEYFILE}")


# --- storages -----------------------------------------------------------------


def pbs_storages() -> dict[str, dict]:
    entries = json.loads(command(["pvesh", "get", "/storage", "--output-format", "json"]))
    return {entry["storage"]: entry for entry in entries if entry.get("type") == "pbs"}


def stanza(name: str) -> str:
    """The `pbs: <name>` block from storage.cfg, so a failed recreate can put it back."""
    lines: list[str] = []
    inside = False
    for line in Path(STORAGE_CFG).read_text(encoding="utf-8").splitlines(keepends=True):
        if line[:1].strip():
            inside = line.split() == ["pbs:", name]
        if inside:
            lines.append(line)
    return "".join(lines)


def password_differs(storage: Storage) -> bool:
    path = Path(STORAGE_PRIV) / f"{storage.name}.pw"
    return not path.is_file() or path.read_text(encoding="utf-8").rstrip("\n") != storage.password


def mutable_options(storage: Storage) -> dict[str, str]:
    options = {
        "username": storage.username,
        "fingerprint": storage.fingerprint,
        "content": "backup",
        "prune-backups": PRUNE_KEEP_ALL,
    }
    if storage.namespace:
        options["namespace"] = storage.namespace
    return options


def flatten(options: dict[str, object]) -> list[str]:
    args: list[str] = []
    for key, value in options.items():
        for item in value if isinstance(value, list) else [value]:
            args += [f"--{key}", str(item)]
    return args


def converge_storage(ctx: InstallContext, storage: Storage, current: dict) -> None:
    """`server` and `datastore` are fixed in PVE's PBS plugin -- `pvesm set` rejects
    them even unchanged -- so only the mutable properties are compared and set."""
    desired = mutable_options(storage)
    delete = ["namespace"] if not storage.namespace and "namespace" in current else []
    new_password = password_differs(storage)
    stale = any(str(current.get(key, "")) != value for key, value in desired.items())
    if not (stale or delete or new_password or ctx.force_update):
        log.sub(f"PBS storage {storage.name} unchanged")
        return
    args = flatten(desired)
    if new_password or ctx.force_update:
        args += ["--password", storage.password]
    if delete:
        args += ["--delete", ",".join(delete)]
    command(["pvesm", "set", storage.name, *args], secret=True)
    log.ok(f"PBS storage {storage.name} updated")


def add_storage(storage: Storage) -> subprocess.CompletedProcess:
    options = {
        "server": storage.server,
        "datastore": storage.datastore,
        **mutable_options(storage),
        "password": storage.password,
    }
    return _run(
        ["pvesm", "add", "pbs", storage.name, *flatten(options)],
        check=False,
        capture_output=True,
        text=True,
    )


def put_back(storage: Storage, previous: str, password: bytes | None) -> None:
    with Path(STORAGE_CFG).open("a", encoding="utf-8") as handle:
        handle.write(previous)
    if password is not None:
        path = Path(STORAGE_PRIV) / f"{storage.name}.pw"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(password)
        path.chmod(0o600)
    log.warn(f"Restored previous definition of PBS storage {storage.name} after failed re-add")


def create_storage(storage: Storage, current: dict | None) -> bool:
    """Add a storage, or recreate one whose fixed properties changed. Returns
    False when the server is not reachable yet, which is left for the next deploy.

    A recreate captures the old stanza and password first and puts them back if
    the add fails: a removed PBS storage means backups stop silently.
    """
    previous, password = "", None
    if current is None:
        log.action(f"Adding PBS storage {storage.name}")
    else:
        log.action(
            f"Recreating PBS storage {storage.name} (datastore {current.get('datastore')} -> "
            f"{storage.datastore}, server {current.get('server')} -> {storage.server})"
        )
        previous = stanza(storage.name)
        pw_path = Path(STORAGE_PRIV) / f"{storage.name}.pw"
        password = pw_path.read_bytes() if pw_path.is_file() else None
        command(["pvesm", "remove", storage.name])

    result = add_storage(storage)
    if result.returncode == 0:
        log.ok(f"PBS storage {storage.name} added")
        return True
    if previous:
        put_back(storage, previous, password)
    # pvesm's own errors name the server, never the password it was given.
    detail = (result.stderr or "").strip()
    if TRANSIENT.search(detail):
        log.warn(
            f"PBS storage {storage.name} is not reachable yet ({detail}); "
            "skipping until next deploy"
        )
        return False
    raise InstallError(f"pvesm add pbs {storage.name} failed: {detail}")


def ensure_encryption(storage: Storage, has_key: bool, ctx: InstallContext) -> None:
    """Only ever set when absent: replacing a key would orphan every snapshot
    already encrypted with it. On a cluster this is pmxcfs-wide."""
    if not storage.encryption:
        return
    if has_key:
        log.sub(f"PBS storage {storage.name} already has an encryption-key; leaving as-is")
        return
    log.action(f"Enabling client-side encryption on PBS storage {storage.name}")
    command(["pvesm", "set", storage.name, "--encryption-key", str(ctx.build_dir / STAGED_KEYFILE)])


def configure_storages(ctx: InstallContext, storages: list[Storage]) -> None:
    install_keyfile(ctx)
    existing = pbs_storages()
    for storage in storages:
        current = existing.get(storage.name)
        fixed = current is not None and (current.get("server"), current.get("datastore")) == (
            storage.server,
            storage.datastore,
        )
        if fixed:
            converge_storage(ctx, storage, current)
            ensure_encryption(storage, bool(current.get("encryption-key")), ctx)
        elif create_storage(storage, current):
            ensure_encryption(storage, False, ctx)


# --- jobs ---------------------------------------------------------------------


def find_job(jobs: list[dict], job: Job) -> dict | None:
    """Jobs are identified by storage plus guest selection, as the bash did, so a
    changed schedule updates the job rather than adding a second one."""
    for current in jobs:
        if current.get("type") != "vzdump" or current.get("storage") != job.storage:
            continue
        if str(current.get("exclude", "")) != job.exclude:
            continue
        if job.vmid and str(current.get("vmid", "")) == job.vmid:
            return current
        if not job.vmid and str(current.get("all", 0)) == "1":
            return current
    return None


def parse_property(value: object, default_key: str | None) -> dict[str, str]:
    """`keep-all=1` / `0` as a dict, to compare with what PVE returns parsed."""
    if isinstance(value, dict):
        return {key: str(item) for key, item in value.items()}
    parsed: dict[str, str] = {}
    for part in str(value or "").split(","):
        if not part:
            continue
        key, sep, item = part.partition("=")
        if sep:
            parsed[key] = item
        elif default_key:
            parsed[default_key] = key
    return parsed


def option_differs(current: dict, key: str, desired: object) -> bool:
    if key in PROPERTY_STRINGS:
        default_key = PROPERTY_STRINGS[key]
        return parse_property(current.get(key), default_key) != parse_property(desired, default_key)
    if isinstance(desired, list):
        return current.get(key) != desired
    return str(current.get(key, "")) != desired


def stale_job_keys(current: dict, options: dict[str, object]) -> list[str]:
    owned = ("all", "vmid", "exclude", "exclude-path")
    return [key for key in owned if key in current and key not in options]


def storage_active(storage: str) -> bool:
    return (
        _run(
            ["pvesm", "status", "--storage", storage], check=False, capture_output=True, text=True
        ).returncode
        == 0
    )


def configure_job(ctx: InstallContext, jobs: list[dict], job: Job) -> None:
    label = f"storage={job.storage} vmid={job.vmid or 'all'}"
    if not storage_active(job.storage):
        log.warn(
            f"Storage {job.storage} is not active yet; skipping backup job {job.index} "
            "until next deploy"
        )
        return
    current = find_job(jobs, job)
    if current is None:
        command(["pvesh", "create", JOBS_ROOT, *flatten(job.options)])
        log.ok(f"Created backup job ({label})")
        return
    delete = stale_job_keys(current, job.options)
    differs = any(option_differs(current, key, value) for key, value in job.options.items())
    if not (differs or delete or ctx.force_update):
        log.sub(f"Backup job {current['id']} unchanged ({label})")
        return
    args = flatten(job.options) + (["--delete", ",".join(delete)] if delete else [])
    command(["pvesh", "set", f"{JOBS_ROOT}/{current['id']}", *args])
    log.ok(
        f"Updated backup job {current['id']} ({label})"
        + (f" (removed: {', '.join(delete)})" if delete else "")
    )


def configure_jobs(ctx: InstallContext, jobs: list[Job]) -> None:
    for job in jobs:
        listing = json.loads(command(["pvesh", "get", JOBS_ROOT, "--output-format", "json"]))
        configure_job(ctx, listing, job)


# --- restore ------------------------------------------------------------------


def lxc_dir(ctx: InstallContext) -> Path:
    return Path(PVE_DIR) / "nodes" / ctx.host / "lxc"


def restore_needed(ctx: InstallContext, restore: Restore) -> bool:
    if ctx.force_update:
        return True
    if not restore.lxc_enabled:
        return False
    return not all((lxc_dir(ctx) / f"{vmid}.conf").is_file() for vmid in restore.vmids)


def client_env(ctx: InstallContext, index: int, repository: str) -> dict[str, str] | None:
    path = ctx.build_dir / f"pbs-{index}.env"
    if not path.is_file():
        log.warn(f"Missing PBS env file: {path}")
        return None
    secret = read_env(path)
    password = secret.get("PBS_PASSWORD") or secret.get("PBS_TOKEN_SECRET", "")
    if not password:
        log.warn(f"PBS credentials missing for {repository}")
        return None
    values = {key: value for key, value in os.environ.items() if not key.startswith("PBS_")}
    values.update(secret)
    values["PBS_PASSWORD"] = password
    if "!" in repository and not secret.get("PBS_TOKEN_SECRET"):
        values["PBS_TOKEN_SECRET"] = password
    return values


def namespace_args(restore: Restore) -> list[str]:
    return ["--ns", restore.namespace] if restore.namespace else []


def latest_snapshot(restore: Restore, repository: str, env: dict[str, str]) -> str | None:
    listing = _run(
        [
            "proxmox-backup-client",
            "snapshots",
            "--repository",
            repository,
            *namespace_args(restore),
            "--output-format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if listing.returncode != 0:
        log.warn(f"Listing snapshots on {repository} failed: {(listing.stderr or '').strip()}")
        return None
    archive = f"{restore.archive}.pxar.didx"
    candidates = [
        snapshot
        for snapshot in json.loads(listing.stdout or "[]")
        if snapshot.get("backup-type") == "host"
        and snapshot.get("backup-id") == restore.backup_id
        and archive in {entry.get("filename") for entry in snapshot.get("files", [])}
    ]
    if not candidates:
        log.warn(
            f"No PBS snapshot of host/{restore.backup_id} holds {restore.archive} on {repository}"
        )
        return None
    newest = max(candidates, key=lambda snapshot: snapshot["backup-time"])
    stamp = datetime.datetime.fromtimestamp(newest["backup-time"], datetime.UTC)
    return f"host/{restore.backup_id}/{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def fetch_from(ctx: InstallContext, restore: Restore, index: int, repository: str) -> bool:
    env = client_env(ctx, index, repository)
    if env is None:
        return False
    log.sub(f"Searching {repository} for backup id '{restore.backup_id}'...")
    snapshot = latest_snapshot(restore, repository, env)
    if snapshot is None:
        return False
    log.sub(f"Restoring snapshot {snapshot} from {repository}...")
    target = Path(RESTORE_ROOT) / "etc-pve"
    shutil.rmtree(RESTORE_ROOT, ignore_errors=True)
    target.mkdir(parents=True)
    keyfile = ["--keyfile", restore.keyfile] if restore.keyfile else []
    result = _run(
        [
            "proxmox-backup-client",
            "restore",
            snapshot,
            f"{restore.archive}.pxar",
            str(target),
            "--repository",
            repository,
            *namespace_args(restore),
            *keyfile,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        log.warn(f"Restore failed from {repository}: {(result.stderr or '').strip()}")
        return False
    return True


def fetch(ctx: InstallContext, restore: Restore) -> Path:
    """Fetch `/etc/pve` from the first destination that has it, into its
    root-only home, and return that copy."""
    for index, repository in enumerate(restore.repositories):
        if repository and fetch_from(ctx, restore, index, repository):
            break
    else:
        raise InstallError(f"No configured PBS destination restored host/{restore.backup_id}")
    try:
        fetched = Path(RESTORE_ROOT) / "etc-pve"
        nested = fetched / "etc" / "pve"
        source = nested if nested.is_dir() else fetched
        latest = Path(RESTORE_OUTPUT) / "latest"
        latest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(latest, ignore_errors=True)
        shutil.copytree(source, latest, symlinks=True)
    finally:
        # A decrypted copy of /etc/pve, private keys included, has no business in /tmp.
        shutil.rmtree(RESTORE_ROOT, ignore_errors=True)
    log.sub(f"Fetched /etc/pve backup to {latest}")
    return latest


def pve_copy(source: Path, dest: Path, mode: int) -> None:
    shutil.copyfile(source, dest)
    try:
        shutil.chown(dest, "root", "www-data")
    except (LookupError, OSError):
        shutil.chown(dest, "root", "root")
    dest.chmod(mode)


def apply_notifications(restored: Path) -> None:
    public, private = restored / "notifications.cfg", restored / "priv" / "notifications.cfg"
    for path in (public, private):
        if not path.is_file():
            log.sub(f"No restored {path.relative_to(restored)} found; skipping auto-apply")
            return
    (Path(PVE_DIR) / "priv").mkdir(parents=True, exist_ok=True)
    pve_copy(public, Path(PVE_DIR) / "notifications.cfg", 0o640)
    pve_copy(private, Path(PVE_DIR) / "priv" / "notifications.cfg", 0o600)
    log.sub("Auto-applied restored notifications config")


def prepared_config(source: Path, autostart: bool) -> str:
    text = source.read_text(encoding="utf-8")
    if autostart:
        return text
    if re.search(r"^onboot:", text, re.MULTILINE):
        return re.sub(r"^onboot:.*$", "onboot: 0", text, flags=re.MULTILINE)
    return f"{text}\nonboot: 0\n"


def missing_volume(config: str) -> str | None:
    for line in config.splitlines():
        key, sep, value = line.partition(":")
        if not sep or not (key == "rootfs" or re.fullmatch(r"mp[0-9]+", key)):
            continue
        volume = value.strip().split(",", 1)[0]
        if (
            ":" in volume
            and _run(
                ["pvesm", "path", volume], check=False, capture_output=True, text=True
            ).returncode
            != 0
        ):
            return volume
    return None


def running(vmid: str) -> bool:
    status = _run(["pct", "status", vmid], check=False, capture_output=True, text=True)
    return "running" in (status.stdout or "")


def restore_lxc(ctx: InstallContext, restore: Restore, restored: Path) -> None:
    if not restore.lxc_enabled:
        log.sub("LXC config restore not enabled; skipping")
        return
    source_dir = restored / "nodes" / ctx.host / "lxc"
    if not source_dir.is_dir():
        raise InstallError(f"No restored LXC config directory found: {source_dir}")
    live_dir = lxc_dir(ctx)
    live_dir.mkdir(parents=True, exist_ok=True)
    for vmid in restore.vmids:
        source, live = source_dir / f"{vmid}.conf", live_dir / f"{vmid}.conf"
        if not source.is_file():
            raise InstallError(f"Restored LXC config missing: {source}")
        desired = prepared_config(source, restore.autostart)
        if live.is_file() and not ctx.force_update:
            if live.read_text(encoding="utf-8") == desired:
                log.sub(f"LXC {vmid} config already restored")
            else:
                log.warn(f"LXC {vmid} config exists; rerun with --force to overwrite")
            continue
        volume = missing_volume(desired)
        if volume:
            raise InstallError(f"LXC {vmid} references missing volume: {volume}")
        staged = ctx.build_dir / f"lxc-{vmid}.conf"
        staged.write_text(desired, encoding="utf-8")
        pve_copy(staged, live, 0o640)
        staged.unlink()
        log.sub(f"Restored LXC {vmid} config")
        if restore.autostart and not running(vmid):
            command(["pct", "start", vmid])


def restore_config(ctx: InstallContext, restore: Restore) -> None:
    if not restore_needed(ctx, restore):
        log.sub("All requested LXC configs already exist; skipping PBS config restore")
        return
    restored = fetch(ctx, restore)
    apply_notifications(restored)
    restore_lxc(ctx, restore, restored)
    log.ok("PBS config restore completed")


# --- entry --------------------------------------------------------------------


Plans = tuple[list[Storage] | None, Restore | None, list[Job] | None]


def preflight(ctx: InstallContext) -> Plans:
    """Every check that can refuse the deploy, before anything is written."""
    has = {
        name: (ctx.build_dir / name).is_file() for name in (STORAGE_PLAN, RESTORE_PLAN, JOBS_PLAN)
    }
    storages = read_storages(ctx) if has[STORAGE_PLAN] else None
    restore = read_restore(ctx) if has[RESTORE_PLAN] else None
    jobs = read_jobs(ctx) if has[JOBS_PLAN] else None
    if storages is not None:
        require_command("pvesm")
    if jobs is not None:
        require_command("pvesh")
        require_command("pvesm")
    if restore is not None:
        require_command("proxmox-backup-client")
        check_restore_keyfile(ctx, restore)
    return storages, restore, jobs


def deploy(ctx: InstallContext) -> None:
    if not ctx.build_dir.is_dir():
        raise InstallError(f"missing build directory: {ctx.build_dir}")
    storages, restore, jobs = preflight(ctx)

    if storages is None:
        log.sub("Standalone PBS storage definitions not configured; skipping")
    else:
        log.action("Configuring standalone PBS storage definitions")
        configure_storages(ctx, storages)

    if restore is None:
        log.sub("PBS config restore not required; skipping")
    else:
        log.action("Checking standalone PVE config recovery")
        restore_config(ctx, restore)

    if jobs is None:
        log.sub("Standalone backup jobs not configured; skipping")
    else:
        log.action("Configuring standalone backup jobs")
        configure_jobs(ctx, jobs)


def install(ctx: InstallContext) -> None:
    log.header("PVE Backup")
    # Staged credentials go on every exit, a refusal included, as the bash's EXIT
    # trap did for two of them.
    try:
        deploy(ctx)
    finally:
        destroy_staged_secrets(ctx)


if __name__ == "__main__":
    run(install, "PVE Backup")
