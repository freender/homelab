"""1Password-backed secret retrieval for the homelab repo.

Materializes secrets into a 24-hour tmpfs cache under /dev/shm so repeated
deploy commands do not consume extra 1Password service-account reads. Nothing
sensitive is written to persistent disk on `riven`. Templates live in
`secrets/templates/<name>.env.tpl` and contain `op://` references that `op inject`
resolves at deploy time.

Usage:
    from . import op_secrets
    path = op_secrets.secret_file("pbs-backup-main")  # /dev/shm/.../*.env

Authentication:
    Service-account token at ~/.config/op/service-account-token (mode 0600).
    The token contents are exported to OP_SERVICE_ACCOUNT_TOKEN for child
    `op` invocations and then dropped from this process's environment.

Offline mode (HOMELAB_OFFLINE=1):
    Returns `secrets/templates/<name>.env.example` if present without
    invoking `op`. Used by `homelab validate` for CI parity.

Cache controls:
    HOMELAB_SECRET_CACHE_TTL=86400 by default. Set to 0 to disable the
    cross-process cache. Use `homelab secrets cache-clear` to revoke early.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CATALOG_PATH = Path("secrets/catalog.yml")
TEMPLATES_DIR = Path("secrets/templates")
TOKEN_PATHS = (
    Path.home() / ".config" / "op" / "homelab.token",
    Path.home() / ".config" / "op" / "service-account-token",
)
TMPFS_BASE = Path("/dev/shm")
TMPFS_PREFIX = "homelab-secrets."
CACHE_PREFIX = "homelab-secret-cache"
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_VAULT = "Homelab"

# Matches a simple env line: KEY=value or KEY="value" or KEY='value'.
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

_session_dir: Path | None = None
_rendered: dict[str, Path] = {}
_session_initialized = False


@dataclass(frozen=True)
class SecretEntry:
    name: str
    template: Path
    example: Path | None
    description: str

    @property
    def filename(self) -> str:
        # Build name on disk, e.g. pbs-backup-main.env
        return f"{self.name}.env"


class OpSecretsError(RuntimeError):
    """Raised for any op-secrets failure (auth, catalog, render)."""


def offline_mode() -> bool:
    return os.environ.get("HOMELAB_OFFLINE", "").lower() in {"1", "true", "yes"}


def load_catalog(root: Path) -> dict[str, SecretEntry]:
    catalog_file = root / CATALOG_PATH
    if not catalog_file.is_file():
        raise OpSecretsError(f"missing secrets catalog: {catalog_file}")

    raw: Any = yaml.safe_load(catalog_file.read_text(encoding="utf-8")) or {}
    secrets_raw = raw.get("secrets") if isinstance(raw, dict) else None
    if not isinstance(secrets_raw, dict) or not secrets_raw:
        raise OpSecretsError(f"{catalog_file}: no `secrets:` entries defined")

    entries: dict[str, SecretEntry] = {}
    for name, value in secrets_raw.items():
        if not isinstance(value, dict):
            raise OpSecretsError(f"{catalog_file}: entry '{name}' must be a mapping")
        template_field = value.get("template")
        if not isinstance(template_field, str) or not template_field.strip():
            raise OpSecretsError(f"{catalog_file}: entry '{name}' missing template path")
        template_path = (root / template_field).resolve()
        if not template_path.is_file():
            raise OpSecretsError(
                f"{catalog_file}: entry '{name}' template not found: {template_path}"
            )
        example_field = value.get("example")
        example_path: Path | None = None
        if isinstance(example_field, str) and example_field.strip():
            candidate = (root / example_field).resolve()
            if candidate.is_file():
                example_path = candidate
        else:
            # Default convention: <template>.example
            default_example = template_path.with_suffix(template_path.suffix + ".example")
            if default_example.is_file():
                example_path = default_example
        description = str(value.get("description", "")).strip()
        entries[name] = SecretEntry(
            name=name,
            template=template_path,
            example=example_path,
            description=description,
        )
    return entries


def _find_token_path() -> Path:
    for path in TOKEN_PATHS:
        if path.is_file():
            return path
    expected = " or ".join(str(path) for path in TOKEN_PATHS)
    raise OpSecretsError(
        "1Password service-account token not found. Create one of: "
        f"{expected}. File must be mode 0600."
    )


def _ensure_secure_token_path(path: Path) -> str:
    if not path.is_file():
        raise OpSecretsError(
            "1Password service-account token not found at "
            f"{path}. Create it (mode 0600) with the homelab service-account "
            "token before running deploy."
        )
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise OpSecretsError(
            f"{path} permissions are too open ({oct(mode)}); expected 0600. "
            f"Run: chmod 600 {path}"
        )
    if info.st_uid != os.getuid():
        raise OpSecretsError(
            f"{path} must be owned by the current user (uid {os.getuid()}); "
            f"found uid {info.st_uid}"
        )
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise OpSecretsError(f"{path} is empty")
    return token


def ensure_op_session() -> None:
    """Authenticate `op` for this process by exporting the service-account token.

    The token is read from a 0600 file and never echoed. We do not keep the
    token in this process beyond exporting it for child `op` invocations.
    """
    global _session_initialized
    if _session_initialized:
        return
    if offline_mode():
        _session_initialized = True
        return
    if shutil.which("op") is None:
        raise OpSecretsError(
            "1Password CLI `op` not found in PATH. Install it before running "
            "deploys that need secrets."
        )
    if "OP_SERVICE_ACCOUNT_TOKEN" not in os.environ:
        token = _ensure_secure_token_path(_find_token_path())
        os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = token
    _session_initialized = True


def _ensure_session_dir() -> Path:
    global _session_dir
    if _session_dir is not None:
        return _session_dir
    if not TMPFS_BASE.is_dir():
        raise OpSecretsError(f"{TMPFS_BASE} not available; cannot render secrets to tmpfs")
    handle = tempfile.mkdtemp(prefix=TMPFS_PREFIX, dir=str(TMPFS_BASE))
    session_path = Path(handle)
    session_path.chmod(0o700)
    _session_dir = session_path
    atexit.register(cleanup)
    _install_signal_handlers()
    return session_path


def cache_ttl_seconds() -> int:
    raw = os.environ.get("HOMELAB_SECRET_CACHE_TTL", str(DEFAULT_CACHE_TTL_SECONDS))
    try:
        ttl = int(raw)
    except ValueError:
        raise OpSecretsError(f"HOMELAB_SECRET_CACHE_TTL must be an integer, got {raw!r}")
    return max(ttl, 0)


def _cache_dir() -> Path:
    if not TMPFS_BASE.is_dir():
        raise OpSecretsError(f"{TMPFS_BASE} not available; cannot cache secrets to tmpfs")
    path = TMPFS_BASE / f"{CACHE_PREFIX}-{os.getuid()}"
    if path.exists() and not path.is_dir():
        raise OpSecretsError(f"secret cache path exists but is not a directory: {path}")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid():
        raise OpSecretsError(f"secret cache directory must be owned by current user: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        path.chmod(0o700)
    return path


def _secret_cache_key(entry: SecretEntry) -> str:
    digest = hashlib.sha256(entry.template.read_bytes()).hexdigest()[:24]
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", entry.name)
    return f"{safe_name}.{digest}.env"


def _cache_path(entry: SecretEntry) -> Path:
    return _cache_dir() / _secret_cache_key(entry)


def _is_cache_fresh(path: Path, ttl_seconds: int) -> bool:
    if ttl_seconds <= 0 or not path.is_file():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds <= ttl_seconds


def _prune_secret_cache(entry: SecretEntry) -> None:
    cache_dir = _cache_dir()
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", entry.name)
    current = _secret_cache_key(entry)
    for path in cache_dir.glob(f"{safe_name}.*.env"):
        if path.name != current:
            _remove_secret_file(path)


def _install_signal_handlers() -> None:
    def _handler(signum: int, _frame: Any) -> None:
        cleanup()
        # Re-raise default behavior so the process actually exits.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not in main thread or signal not supported; ignore.
            pass


def cleanup() -> None:
    """Shred and remove the session tmpfs directory. Idempotent."""
    global _session_dir
    if _session_dir is None:
        return
    target = _session_dir
    _session_dir = None
    _rendered.clear()
    if not target.exists():
        return
    shred = shutil.which("shred")
    for file_path in sorted(target.rglob("*"), reverse=True):
        if file_path.is_file():
            try:
                if shred:
                    subprocess.run(
                        [shred, "-u", "-n", "1", str(file_path)],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    file_path.unlink(missing_ok=True)
            except OSError:
                pass
    try:
        shutil.rmtree(target, ignore_errors=True)
    except OSError:
        pass


def _remove_secret_file(path: Path) -> None:
    shred = shutil.which("shred")
    try:
        if shred:
            subprocess.run(
                [shred, "-u", "-n", "1", str(path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def clear_cache() -> None:
    """Shred and remove the cross-process tmpfs secret cache."""
    _rendered.clear()
    path = TMPFS_BASE / f"{CACHE_PREFIX}-{os.getuid()}"
    if not path.exists():
        return
    if not path.is_dir():
        raise OpSecretsError(f"secret cache path exists but is not a directory: {path}")
    info = path.stat()
    if info.st_uid != os.getuid():
        raise OpSecretsError(f"refusing to remove cache not owned by current user: {path}")
    for file_path in sorted(path.rglob("*"), reverse=True):
        if file_path.is_file():
            _remove_secret_file(file_path)
    shutil.rmtree(path, ignore_errors=True)


def cache_info() -> dict[str, object]:
    path = TMPFS_BASE / f"{CACHE_PREFIX}-{os.getuid()}"
    files = []
    if path.is_dir():
        now = time.time()
        for file_path in sorted(path.glob("*.env")):
            stat_result = file_path.stat()
            files.append(
                {
                    "name": file_path.name,
                    "age_seconds": int(now - stat_result.st_mtime),
                    "size": stat_result.st_size,
                }
            )
    return {
        "path": str(path),
        "ttl_seconds": cache_ttl_seconds(),
        "files": files,
    }


def _render_with_op(template: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    # Pre-create destination with strict perms before op writes to it.
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    result = subprocess.run(
        [
            "op",
            "inject",
            "--force",
            "--in-file",
            str(template),
            "--out-file",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # op writes useful diagnostics to stderr; surface them but never the
        # template body or rendered file contents.
        message = (result.stderr or result.stdout or "").strip().splitlines()
        snippet = " | ".join(message[-3:]) if message else "no output"
        destination.unlink(missing_ok=True)
        raise OpSecretsError(
            f"op inject failed for template {template.name}: {snippet}"
        )
    destination.chmod(0o600)


def secret_file(root: Path, name: str) -> Path:
    """Return path to a rendered secret file for the given catalog entry name.

    On first call the file is materialized into the session tmpfs dir.
    Subsequent calls return the cached path.
    """
    is_offline = offline_mode()
    if not is_offline and name in _rendered:
        return _rendered[name]

    catalog = load_catalog(root)
    entry = catalog.get(name)
    if entry is None:
        raise OpSecretsError(f"unknown secret '{name}' (not in {CATALOG_PATH})")

    if is_offline:
        if entry.example is None:
            raise OpSecretsError(
                f"offline mode: no example file for secret '{name}'. "
                f"Create {entry.template}.example to support offline validation."
            )
        return entry.example

    ensure_op_session()
    ttl_seconds = cache_ttl_seconds()
    if ttl_seconds > 0:
        cache_path = _cache_path(entry)
        if _is_cache_fresh(cache_path, ttl_seconds):
            _rendered[name] = cache_path
            return cache_path
        _prune_secret_cache(entry)
        temp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
        try:
            _render_with_op(entry.template, temp_path)
            os.replace(temp_path, cache_path)
            cache_path.chmod(0o600)
        finally:
            if temp_path.exists():
                _remove_secret_file(temp_path)
        _rendered[name] = cache_path
        return cache_path

    session = _ensure_session_dir()
    destination = session / entry.filename
    _render_with_op(entry.template, destination)
    _rendered[name] = destination
    return destination


def doctor(root: Path, names: Iterable[str] | None = None) -> int:
    """Verify each catalog entry resolves via `op inject` without printing values.

    Returns 0 on success, non-zero if any entry fails.
    """
    catalog = load_catalog(root)
    targets = list(names) if names else list(catalog.keys())
    failures: list[tuple[str, str]] = []
    if offline_mode():
        for name in targets:
            entry = catalog.get(name)
            if entry is None:
                failures.append((name, "not in catalog"))
                continue
            if entry.example is None:
                failures.append((name, "missing offline example"))
                continue
            print(f"  [offline] {name}: example OK ({entry.example.name})")
        return 0 if not failures else 1

    try:
        ensure_op_session()
    except OpSecretsError as exc:
        print(f"  authentication failed: {exc}", file=sys.stderr)
        return 1

    session = _ensure_session_dir()
    try:
        for name in targets:
            entry = catalog.get(name)
            if entry is None:
                failures.append((name, "not in catalog"))
                print(f"  FAIL  {name}: not in catalog")
                continue
            destination = session / entry.filename
            try:
                _render_with_op(entry.template, destination)
                print(f"  OK    {name}")
            except OpSecretsError as exc:
                failures.append((name, str(exc)))
                print(f"  FAIL  {name}: {exc}")
    finally:
        cleanup()

    if failures:
        print(f"\n{len(failures)} secret(s) failed to resolve.", file=sys.stderr)
        return 1
    return 0


def render_all(root: Path) -> Path:
    """Materialize every catalog secret into the session tmpfs and return the dir.

    Used by `homelab secrets render` for manual inspection. Cleanup still runs
    at process exit; the returned path will be removed.
    """
    catalog = load_catalog(root)
    for name in catalog:
        secret_file(root, name)
    if _session_dir is None:
        # Offline mode does not allocate a tmpfs dir; surface where files live.
        if not offline_mode():
            return _cache_dir()
        return root / TEMPLATES_DIR
    return _session_dir


def list_secret_names(root: Path) -> list[str]:
    return sorted(load_catalog(root).keys())


# ---------------------------------------------------------------------------
# Env-file parsing helpers.
#
# Used by modules that read a rendered secret file back in (keepalived,
# pve-autoinstall, pve-backup, pve-http-boot, pve-notifications,
# pve-postinstall-webhook).
# ---------------------------------------------------------------------------


def _strip_env_value(raw: str) -> str:
    """Strip surrounding quotes from an env value if both ends match."""
    value = raw.strip()
    # Remove trailing inline comment for unquoted values, conservative.
    if value and value[0] not in {'"', "'"}:
        # Only strip a comment if preceded by whitespace.
        comment = re.search(r"\s+#", value)
        if comment is not None:
            value = value[: comment.start()].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE env file into a dict. Comments and blank lines ignored."""
    if not path.is_file():
        raise OpSecretsError(f"env file not found: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(raw_line)
        if match is None:
            raise OpSecretsError(f"{path}:{line_number}: cannot parse env line")
        key, raw_value = match.group(1), match.group(2)
        values[key] = _strip_env_value(raw_value)
    return values
