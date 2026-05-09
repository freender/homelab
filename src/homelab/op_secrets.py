"""1Password-backed secret retrieval for the homelab repo.

Materializes secrets onto a tmpfs build directory under /dev/shm so nothing
sensitive is ever written to persistent disk on `riven`. Templates live in
`secrets/templates/<name>.env.tpl` and contain `op://` references that
`op inject` resolves at deploy time.

Usage:
    from . import op_secrets
    path = op_secrets.secret_file("pbs-backup-main")  # /dev/shm/.../pbs-backup-main.env

Authentication:
    Service-account token at ~/.config/op/homelab.token (mode 0600).
    The token contents are exported to OP_SERVICE_ACCOUNT_TOKEN for child
    `op` invocations and then dropped from this process's environment.

Offline mode (HOMELAB_OFFLINE=1):
    Returns `secrets/templates/<name>.env.example` if present without
    invoking `op`. Used by `homelab validate` for CI parity.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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
DEFAULT_VAULT = "Homelab"

# Matches `{{ op://Vault/Item/field }}` (single-line, with optional whitespace).
_OP_REF_RE = re.compile(r"\{\{\s*op://([^/}]+)/(.+)/([^/\s}]+)\s*\}\}")
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
    if name in _rendered:
        return _rendered[name]

    catalog = load_catalog(root)
    entry = catalog.get(name)
    if entry is None:
        raise OpSecretsError(f"unknown secret '{name}' (not in {CATALOG_PATH})")

    if offline_mode():
        if entry.example is None:
            raise OpSecretsError(
                f"offline mode: no example file for secret '{name}'. "
                f"Create {entry.template}.example to support offline validation."
            )
        _rendered[name] = entry.example
        return entry.example

    ensure_op_session()
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
        return root / TEMPLATES_DIR
    return _session_dir


def list_secret_names(root: Path) -> list[str]:
    return sorted(load_catalog(root).keys())


# ---------------------------------------------------------------------------
# Bootstrap helpers (one-time migration from secrets/*.env into 1Password).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldBinding:
    """One env-key -> op-field pairing for a single template."""

    env_key: str
    op_vault: str
    op_item: str
    op_field: str


@dataclass(frozen=True)
class TemplatePlan:
    """Computed bootstrap plan for a single secret entry."""

    secret_name: str
    op_item: str
    op_vault: str
    bindings: tuple[FieldBinding, ...]
    legacy_env_path: Path


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


def parse_template_bindings(template_path: Path) -> tuple[tuple[str, str, str, str], ...]:
    """Extract `(env_key, op_vault, op_item, op_field)` tuples from a template.

    The env_key is the LHS of the line containing the op:// reference. We do
    not attempt to support multiple references per line; templates in this
    repo follow KEY={{ op://... }} (one per line).
    """
    bindings: list[tuple[str, str, str, str]] = []
    for raw_line in template_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _OP_REF_RE.search(raw_line)
        if match is None:
            continue
        env_match = _ENV_LINE_RE.match(raw_line)
        if env_match is None:
            raise OpSecretsError(
                f"{template_path}: line with op:// reference is not KEY=...: {raw_line!r}"
            )
        env_key = env_match.group(1)
        op_vault, op_item, op_field = match.group(1), match.group(2), match.group(3)
        bindings.append((env_key, op_vault, op_item, op_field))
    if not bindings:
        raise OpSecretsError(f"{template_path}: no op:// references found")
    return tuple(bindings)


def _legacy_env_filename(secret_name: str) -> str:
    """Map catalog name back to the legacy `secrets/<name>.env` filename.

    Today the catalog name and the legacy filename match 1-to-1 (e.g.
    `pbs-backup-main` <-> `pbs-backup-main.env`).
    """
    return f"{secret_name}.env"


def build_template_plan(root: Path, secret_name: str) -> TemplatePlan:
    catalog = load_catalog(root)
    entry = catalog.get(secret_name)
    if entry is None:
        raise OpSecretsError(f"unknown secret '{secret_name}'")

    raw_bindings = parse_template_bindings(entry.template)
    op_items = {item for _, _, item, _ in raw_bindings}
    op_vaults = {vault for _, vault, _, _ in raw_bindings}
    if len(op_items) != 1:
        raise OpSecretsError(
            f"{entry.template}: bootstrap requires exactly one op item per template "
            f"(found {sorted(op_items)})"
        )
    if len(op_vaults) != 1:
        raise OpSecretsError(
            f"{entry.template}: bootstrap requires exactly one op vault per template "
            f"(found {sorted(op_vaults)})"
        )
    op_item = next(iter(op_items))
    op_vault = next(iter(op_vaults))

    bindings = tuple(
        FieldBinding(env_key=env_key, op_vault=vault, op_item=item, op_field=field)
        for env_key, vault, item, field in raw_bindings
    )
    legacy_path = root / "secrets" / _legacy_env_filename(secret_name)
    return TemplatePlan(
        secret_name=secret_name,
        op_item=op_item,
        op_vault=op_vault,
        bindings=bindings,
        legacy_env_path=legacy_path,
    )


def _op_run(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke `op` with consistent flags. Returns the CompletedProcess."""
    return subprocess.run(
        ["op", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _op_item_exists(vault: str, item: str) -> bool:
    result = _op_run(
        ["item", "get", item, "--vault", vault, "--format", "json"]
    )
    return result.returncode == 0


def _op_error_snippet(result: subprocess.CompletedProcess) -> str:
    message = (result.stderr or result.stdout or "").strip().splitlines()
    return " | ".join(message[-3:]) if message else "no output"


def _op_item_payload(item: str, fields: dict[str, str]) -> dict[str, Any]:
    return {
        "title": item,
        "category": "LOGIN",
        "fields": [
            {
                "id": field_name,
                "label": field_name,
                "type": "CONCEALED",
                "value": value,
            }
            for field_name, value in fields.items()
        ],
    }


def _write_op_template(item: str, fields: dict[str, str]) -> Path:
    """Write an op item JSON template into tmpfs and return its path."""
    session = _ensure_session_dir()
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="op-item.",
        suffix=".json",
        dir=str(session),
        delete=False,
    )
    with handle:
        json.dump(_op_item_payload(item, fields), handle)
        handle.write("\n")
    template_path = Path(handle.name)
    template_path.chmod(0o600)
    return template_path


def _op_item_create(vault: str, item: str, fields: dict[str, str]) -> None:
    """Create a Login-category item with `concealed` fields for each entry.

    Values are written to a tmpfs JSON template, not process argv. This avoids
    exposing secrets through `ps` while still using the supported `op item
    create --template <file>` path.
    """
    template_path = _write_op_template(item, fields)
    result = _op_run(
        [
            "item",
            "create",
            "--vault",
            vault,
            "--template",
            str(template_path),
            "--format",
            "json",
        ]
    )
    if result.returncode != 0:
        raise OpSecretsError(
            f"op item create failed for '{item}': {_op_error_snippet(result)}"
        )


def _op_item_edit(vault: str, item: str, fields: dict[str, str]) -> None:
    """Update field values on an existing item using a tmpfs JSON template."""
    template_path = _write_op_template(item, fields)
    result = _op_run(
        [
            "item",
            "edit",
            item,
            "--vault",
            vault,
            "--template",
            str(template_path),
            "--format",
            "json",
        ]
    )
    if result.returncode != 0:
        raise OpSecretsError(
            f"op item edit failed for '{item}': {_op_error_snippet(result)}"
        )


def bootstrap(
    root: Path,
    names: Iterable[str] | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> int:
    """Create or update 1Password items from existing legacy `secrets/*.env` files.

    Skips items that already exist unless `--force` is set. Never echoes
    secret values; only item / field counts and outcomes are printed.

    Returns 0 on success, non-zero if any item failed.
    """
    if offline_mode():
        print("bootstrap requires online mode; HOMELAB_OFFLINE is set", file=sys.stderr)
        return 1

    catalog = load_catalog(root)
    targets = list(names) if names else list(catalog.keys())

    if not dry_run:
        try:
            ensure_op_session()
        except OpSecretsError as exc:
            print(f"  authentication failed: {exc}", file=sys.stderr)
            return 1

    failures: list[tuple[str, str]] = []
    skipped: list[str] = []
    created: list[str] = []
    updated: list[str] = []

    for name in targets:
        if name not in catalog:
            failures.append((name, "not in catalog"))
            print(f"  FAIL  {name}: not in catalog")
            continue

        try:
            plan = build_template_plan(root, name)
        except OpSecretsError as exc:
            failures.append((name, str(exc)))
            print(f"  FAIL  {name}: {exc}")
            continue

        if not plan.legacy_env_path.is_file():
            failures.append((name, f"legacy env file missing: {plan.legacy_env_path}"))
            print(f"  FAIL  {name}: legacy env file missing")
            continue

        try:
            env_values = parse_env_file(plan.legacy_env_path)
        except OpSecretsError as exc:
            failures.append((name, str(exc)))
            print(f"  FAIL  {name}: {exc}")
            continue

        fields: dict[str, str] = {}
        missing_keys: list[str] = []
        for binding in plan.bindings:
            value = env_values.get(binding.env_key)
            if value is None or value == "":
                missing_keys.append(binding.env_key)
                continue
            fields[binding.op_field] = value
        if missing_keys:
            failures.append((name, f"missing env keys: {', '.join(missing_keys)}"))
            print(f"  FAIL  {name}: missing env keys: {', '.join(missing_keys)}")
            continue

        exists = False if dry_run else _op_item_exists(plan.op_vault, plan.op_item)
        action = "plan" if dry_run else "update" if exists else "create"
        prefix = "[dry-run] " if dry_run else ""
        if exists and not force:
            skipped.append(name)
            print(
                f"  SKIP  {name}: '{plan.op_item}' already exists in vault "
                f"'{plan.op_vault}' (use --force to overwrite)"
            )
            continue

        print(
            f"  {prefix}{action.upper()}  {name}: vault='{plan.op_vault}' "
            f"item='{plan.op_item}' fields={len(fields)}"
        )
        if dry_run:
            continue

        try:
            if exists:
                _op_item_edit(plan.op_vault, plan.op_item, fields)
                updated.append(name)
            else:
                _op_item_create(plan.op_vault, plan.op_item, fields)
                created.append(name)
        except OpSecretsError as exc:
            failures.append((name, str(exc)))
            print(f"  FAIL  {name}: {exc}")

    print()
    print(
        f"Summary: created={len(created)} updated={len(updated)} "
        f"skipped={len(skipped)} failed={len(failures)}"
    )
    if failures:
        return 1
    if dry_run:
        return 0

    print("\nVerifying with op inject...")
    return doctor(root, targets)
