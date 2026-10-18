"""Shared helpers: load example signals and derive variants from them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "signals"
SCHEMA_FILE = ROOT / "schemas" / "signal-contract-1.0.schema.json"

DROP = object()  # sentinel: remove the key


def example_files(group: str) -> list[Path]:
    return sorted((EXAMPLES / group).glob("*.md"))


def example_text(group: str, name: str) -> str:
    return (EXAMPLES / group / name).read_text(encoding="utf-8")


def split(text: str) -> tuple[dict[str, Any], str]:
    """Return (front matter as dict, body) of an example document."""
    _, front, body = text.split("---\n", 2)
    return yaml.safe_load(front), body


def render(data: dict[str, Any], body: str = "\n## Rationale\nTest.\n") -> str:
    front = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return f"---\n{front}---\n{body}"


def build(name: str, *, body: str | None = None, group: str = "valid", **overrides: Any) -> str:
    """Load an example and apply overrides; keys may be dotted ('contract.dte_min')."""
    data, original_body = split(example_text(group, name))
    for key, value in overrides.items():
        target = data
        *parents, last = key.split(".")
        for part in parents:
            target = target[part]
        if value is DROP:
            target.pop(last, None)
        else:
            target[last] = value
    return render(data, original_body if body is None else body)


def front_matter(name: str, group: str = "valid") -> dict[str, Any]:
    return split(example_text(group, name))[0]
