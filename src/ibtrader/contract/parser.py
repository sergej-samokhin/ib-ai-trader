"""Markdown + YAML front matter -> validated Signal. Content is data, never code."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from ibtrader.contract.models import Signal

_SIGNAL_ADAPTER: TypeAdapter[Any] = TypeAdapter(Signal)
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"


@dataclass
class FieldError:
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message}


@dataclass
class SignalParseError(Exception):
    errors: list[FieldError] = field(default_factory=list)

    def __str__(self) -> str:
        return "; ".join(f"{e.field}: {e.message}" for e in self.errors)


@dataclass
class ParsedSignal:
    signal: Any  # TradeSignal | RuleSignal | ExitSignal
    front_matter: dict[str, Any]
    body: str


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader without aliases, duplicate keys or implicit timestamp conversion."""

    def compose_node(self, parent, index):  # type: ignore[override]
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.constructor.ConstructorError(
                None, None, "YAML aliases/anchors are not allowed", event.start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key '{key}'", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


# Keep timestamps as strings so pydantic (strict, JSON mode) does all date parsing.
_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != _TIMESTAMP_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def split_front_matter(raw: str) -> tuple[str, str]:
    text = raw.removeprefix("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise SignalParseError([FieldError("$", "document must start with '---' front matter")])
    end = text.find("\n---\n", 3)
    if end == -1:
        if text.rstrip().endswith("\n---"):
            end = text.rstrip().rfind("\n---")
        else:
            raise SignalParseError([FieldError("$", "front matter is not closed with '---'")])
    front = text[4:end]
    body = text[end + 5 :] if end + 5 <= len(text) else ""
    return front, body


def _loc_to_field(loc: tuple[Any, ...], kind: Any) -> str:
    parts = [str(p) for p in loc]
    if parts and kind is not None and parts[0] == str(kind):
        parts = parts[1:]  # discriminated-union tag
    return ".".join(parts) or "$"


def validate_front_matter(data: Any) -> Any:
    if not isinstance(data, dict):
        raise SignalParseError([FieldError("$", "front matter must be a mapping")])
    try:
        payload = json.dumps(data, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SignalParseError([FieldError("$", f"unsupported value: {exc}")]) from None
    try:
        return _SIGNAL_ADAPTER.validate_json(payload, strict=True)
    except ValidationError as exc:
        kind = data.get("kind")
        errors = []
        for err in exc.errors(include_url=False):
            msg = err["msg"].removeprefix("Value error, ")
            if err["type"] == "extra_forbidden":
                msg = "unknown key"
            elif err["type"] == "missing":
                msg = "required key missing"
            errors.append(FieldError(_loc_to_field(err["loc"], kind), msg))
        raise SignalParseError(errors) from None


def parse_signal(raw: str) -> ParsedSignal:
    front, body = split_front_matter(raw)
    try:
        data = yaml.load(front, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        reason = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise SignalParseError([FieldError("$", f"invalid YAML: {reason}")]) from None
    signal = validate_front_matter(data)
    return ParsedSignal(signal=signal, front_matter=data, body=body)
