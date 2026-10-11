"""Replaces every `*_changed=false` variable and the bash `rc=` idiom."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChangeSet:
    """What changed this run, by name. A function that changes nothing just
    returns -- nothing has to be guarded with an `if rc -eq 0` check."""

    _names: set[str] = field(default_factory=set)

    def record(self, name: str) -> None:
        self._names.add(name)

    def any(self) -> bool:
        return bool(self._names)

    def touched(self, *names: str) -> bool:
        return any(name in self._names for name in names)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._names))
