"""Typed reads of the `build/<host>/env` file -- the replacement for `require_env`
and for the `VAR="false"` / `source "$BUILD_DIR/env"` default-then-override dance
every installer opens with.

`apt-upgrade` (freender/homelab-ops#30) is the first module to consume an env file
at all, so this arrives with it per #31 decision 3. `main._parse_env_file` has
existed since #33 but nothing read its output: keepalived ships no env file and
`base-packages` uses the process environment (`ctx.deploy_env`) instead.

Two things the bash could not do, both of which it got wrong in the same place:

* **Absent and empty are different.** `require_env` treated `-z "${!name}"` as
  missing, so `SCHEDULE=""` and a truncated file that never wrote `SCHEDULE` at
  all produced the same error. Here `require` distinguishes them, because they
  have different causes: one is a bad value in `hosts.conf`, the other is a
  failed render.
* **An unparseable flag is not `false`.** `[[ "$AUTOUPGRADE" == "true" ]]` maps
  every typo -- `True`, `yes`, `ture` -- to false, silently disabling the timer.
  `flag()` accepts the same spellings `normalize_bool` does on the orchestrator
  side and raises on anything else, so a typo fails the deploy instead of quietly
  turning a feature off.
"""

from __future__ import annotations

from .context import InstallContext
from .errors import InstallError

# Deliberately the same accepted spellings as `module_support.normalize_bool`,
# which parses the *other* end of this wire. A value that `hosts.conf` accepts
# and this rejects would deploy fine and then fail on the host.
_TRUE = frozenset({"true", "yes", "1", "on"})
_FALSE = frozenset({"false", "no", "0", "off"})


def require(ctx: InstallContext, *names: str) -> None:
    """Raise unless every name is present in the env file and non-empty."""
    absent = [name for name in names if name not in ctx.env]
    empty = [name for name in names if name in ctx.env and not ctx.env[name].strip()]

    problems: list[str] = []
    if absent:
        problems.append(f"missing: {', '.join(absent)}")
    if empty:
        problems.append(f"empty: {', '.join(empty)}")
    if problems:
        raise InstallError(
            f"incomplete env file at {ctx.build_dir / 'env'} ({'; '.join(problems)}); "
            "refusing to run with an ambiguous config"
        )


def text(ctx: InstallContext, name: str, default: str) -> str:
    """Read a string, falling back to `default` when absent or empty."""
    value = ctx.env.get(name, "").strip()
    return value or default


def flag(ctx: InstallContext, name: str, default: bool = False) -> bool:
    """Read a boolean. Absent or empty yields `default`; an unrecognised value
    raises rather than being coerced to false."""
    raw = ctx.env.get(name, "").strip()
    if not raw:
        return default
    normalized = raw.lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise InstallError(f"{name} must be true or false in {ctx.build_dir / 'env'}, got {raw!r}")
