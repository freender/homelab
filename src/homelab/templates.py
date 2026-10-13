from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def render_template_text(template: Path, **context: Any) -> str:
    environment = Environment(
        loader=FileSystemLoader(str(template.parent)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    return environment.get_template(template.name).render(**context)


def render_template(template: Path, output: Path, **context: Any) -> None:
    rendered = render_template_text(template, **context)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
