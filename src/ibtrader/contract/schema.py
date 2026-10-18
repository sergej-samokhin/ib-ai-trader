"""JSON Schema export of the Signal Contract (for upstream agents to self-test)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter

from ibtrader.contract.models import CONTRACT_VERSION, Signal


def signal_json_schema() -> dict[str, Any]:
    schema = TypeAdapter(Signal).json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://ibtrader.local/schemas/signal-contract-{CONTRACT_VERSION}.json"
    schema["title"] = f"IBKR Options Signal Contract v{CONTRACT_VERSION}"
    schema["description"] = (
        "Front matter of a signal document (YAML converted to JSON). Semantic rules that "
        "JSON Schema cannot express (e.g. short_leg_delta < long_leg_delta, the condition "
        "grammar, valid_until > created_at) are enforced by the gateway; see SIGNAL_CONTRACT.md."
    )
    return schema


def render_schema() -> str:
    return json.dumps(signal_json_schema(), indent=2, sort_keys=True) + "\n"
