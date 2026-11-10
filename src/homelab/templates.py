from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def render_template_text(
    template: Path, search_path: Sequence[Path] = (), **context: Any
) -> str:
    """`search_path` adds directories after the template's own for `import` and
    `include`, so one module can render a partial another module owns."""
    environment = Environment(
        loader=FileSystemLoader([str(template.parent), *map(str, search_path)]),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    return environment.get_template(template.name).render(**context)


def render_template(
    template: Path, output: Path, search_path: Sequence[Path] = (), **context: Any
) -> None:
    rendered = render_template_text(template, search_path, **context)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
