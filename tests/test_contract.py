"""Signal Contract v1.0: parser, strict models and the published JSON Schema."""

from __future__ import annotations

import json

import pytest
import yaml
from jsonschema import Draft202012Validator

from helpers_contract import DROP, SCHEMA_FILE, build, example_files, front_matter
from ibtrader.contract import (
    ExitSignal,
    RuleSignal,
    SignalParseError,
    TradeSignal,
    parse_signal,
)
from ibtrader.contract.parser import split_front_matter
from ibtrader.contract.schema import render_schema

TRADE = "trade-bull-call-spread.md"
PUT = "trade-long-put.md"
RULE = "rule-condition.md"
SCHEDULE = "rule-schedule.md"
CLOSE = "exit-close.md"
REDUCE = "exit-reduce.md"


def fields_of(raw: str) -> dict[str, str]:
    with pytest.raises(SignalParseError) as exc:
        parse_signal(raw)
    return {e.field: e.message for e in exc.value.errors}


def assert_field_error(raw: str, field: str, fragment: str | None = None) -> None:
    errors = fields_of(raw)
    assert field in errors, errors
    if fragment:
        assert fragment in errors[field]


# -- examples --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path", example_files("valid") + example_files("adversarial"), ids=lambda p: p.name
)
def test_valid_and_adversarial_examples_parse(path):
    parsed = parse_signal(path.read_text(encoding="utf-8"))
    assert parsed.signal.signal_id == parsed.front_matter["signal_id"]


@pytest.mark.parametrize("path", example_files("invalid"), ids=lambda p: p.name)
def test_invalid_examples_fail_with_field_level_error(path):
    with pytest.raises(SignalParseError) as exc:
        parse_signal(path.read_text(encoding="utf-8"))
    assert exc.value.errors
    for err in exc.value.errors:
        assert err.field and err.message


def test_example_groups_are_not_empty():
    assert len(example_files("valid")) >= 6
    assert len(example_files("invalid")) >= 9
    assert len(example_files("adversarial")) >= 1


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        (TRADE, TradeSignal),
        (PUT, TradeSignal),
        (RULE, RuleSignal),
        (SCHEDULE, RuleSignal),
        (CLOSE, ExitSignal),
        (REDUCE, ExitSignal),
    ],
)
def test_kind_discriminates_model(name, cls):
    assert isinstance(parse_signal(build(name)).signal, cls)


def test_parsed_values_and_body():
    parsed = parse_signal(build(TRADE))
    sig = parsed.signal
    assert sig.underlying == "NVDA"
    assert sig.strategy.direction == "bullish"
    assert sig.strategy.is_spread
    assert sig.contract.short_leg_delta == 0.2
    assert sig.created_at.utcoffset().total_seconds() == 0
    assert "## Rationale" in parsed.body
    assert parsed.front_matter["kind"] == "trade"


def test_specific_invalid_example_fields():
    from helpers_contract import example_text

    unknown = fields_of(example_text("invalid", "unknown-key.md"))
    assert unknown == {"sizing.leverage": "unknown key"}
    missing = fields_of(example_text("invalid", "missing-valid-until.md"))
    assert missing == {"valid_until": "required key missing"}
    assert "contract.dte_min" in fields_of(example_text("invalid", "wrong-type.md"))
    assert "strategy" in fields_of(example_text("invalid", "naked-short-call.md"))
    assert "trigger" in fields_of(example_text("invalid", "code-injection-condition.md"))


# -- unknown / missing / wrong types ---------------------------------------------------
def test_unknown_top_level_key():
    assert fields_of(build(TRADE, leverage=3)) == {"leverage": "unknown key"}


def test_unknown_nested_key():
    assert fields_of(build(TRADE, **{"order.tif": "day"})) == {"order.tif": "unknown key"}


@pytest.mark.parametrize(
    ("name", "key"),
    [
        (TRADE, "producer"),
        (TRADE, "confidence"),
        (TRADE, "created_at"),
        (TRADE, "sizing"),
        (TRADE, "exit.stop_loss_pct"),
        (RULE, "trigger"),
        (RULE, "max_entries_per_day"),
        (CLOSE, "target_signal_id"),
        (CLOSE, "action"),
    ],
)
def test_missing_required_key(name, key):
    assert fields_of(build(name, **{key: DROP})) == {key: "required key missing"}


def test_missing_kind_is_rejected():
    errors = fields_of(build(TRADE, kind=DROP))
    assert errors  # discriminator missing


def test_unknown_kind_is_rejected():
    assert fields_of(build(TRADE, kind="spreadsheet"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract.dte_min": "21"},
        {"contract.long_leg_delta": "0.4"},
        {"sizing.max_contracts": "2"},
        {"sizing.max_contracts": 2.0},
        {"sizing.max_contracts": True},
        {"confidence": "0.6"},
        {"version": "1"},
        {"version": 1.0},
        {"version": True},
        {"signal_id": 12345},
        {"contract_version": 1.0},
    ],
)
def test_strict_types_no_coercion(overrides):
    assert fields_of(build(TRADE, **overrides))


def test_numeric_timestamp_rejected():
    assert fields_of(build(TRADE, created_at=1760881200))


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": 1.01},
        {"confidence": -0.1},
        {"contract.long_leg_delta": 0.01},
        {"contract.dte_max": 400},
        {"order.max_reprice_steps": 11},
        {"sizing.max_premium_usd": 0},
        {"sizing.max_premium_usd": 100_001},
        {"sizing.max_contracts": 0},
        {"sizing.max_contracts": 101},
        {"exit.stop_loss_pct": 101},
        {"exit.take_profit_pct": 0},
        {"underlying": "nvda"},
        {"underlying": "TOOLONG"},
        {"signal_id": "ab"},
        {"signal_id": "Has Space"},
        {"producer": "Momentum Agent"},
        {"contract_version": "2.0"},
    ],
)
def test_bounds_and_patterns(overrides):
    assert fields_of(build(TRADE, **overrides))


def test_order_must_be_limit_at_mid():
    assert fields_of(build(TRADE, **{"order.type": "market"}))
    assert fields_of(build(TRADE, **{"order.price": "ask"}))


def test_dte_window_must_be_ordered():
    assert_field_error(
        build(TRADE, **{"contract.dte_min": 50, "contract.dte_max": 40}), "contract", "dte_min"
    )


def test_valid_until_must_follow_created_at():
    raw = build(TRADE, valid_until="2026-10-19T13:40:00Z")
    assert_field_error(raw, "$", "valid_until")
    assert fields_of(build(TRADE, valid_until="2026-10-19T10:00:00Z"))


def test_class_dot_ticker_allowed():
    assert parse_signal(build(TRADE, underlying="BRK.B")).signal.underlying == "BRK.B"


# -- strategies ------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["short_call", "short_put", "naked_call", "iron_condor"])
def test_naked_short_and_unknown_strategies_rejected(strategy):
    assert "strategy" in fields_of(build(PUT, strategy=strategy))


@pytest.mark.parametrize("strategy", ["short_call", "short_put", "straddle"])
def test_naked_short_strategies_rejected_for_rules(strategy):
    assert "strategy" in fields_of(build(RULE, strategy=strategy))


@pytest.mark.parametrize(
    ("strategy", "right", "direction", "spread"),
    [
        ("long_call", "C", "bullish", False),
        ("long_put", "P", "bearish", False),
        ("bull_call_spread", "C", "bullish", True),
        ("bear_put_spread", "P", "bearish", True),
    ],
)
def test_strategy_properties(strategy, right, direction, spread):
    from ibtrader.contract import Strategy

    s = Strategy(strategy)
    assert (s.right, s.direction, s.is_spread) == (right, direction, spread)


# -- short_leg_delta rules -------------------------------------------------------------
def test_spread_requires_short_leg_delta():
    assert_field_error(build(TRADE, **{"contract.short_leg_delta": DROP}), "$", "short_leg_delta")


def test_spread_short_delta_must_be_smaller():
    assert_field_error(build(TRADE, **{"contract.short_leg_delta": 0.5}), "$", "smaller")
    assert fields_of(build(TRADE, **{"contract.short_leg_delta": 0.4}))  # equal is rejected


def test_single_leg_forbids_short_leg_delta():
    assert_field_error(build(PUT, **{"contract.short_leg_delta": 0.1}), "$", "not allowed")


def test_single_leg_without_short_delta_ok():
    assert parse_signal(build(PUT)).signal.contract.short_leg_delta is None


def test_rule_spread_delta_rules_apply_too():
    assert fields_of(build(RULE, **{"contract.short_leg_delta": DROP}))
    assert fields_of(build(RULE, **{"contract.short_leg_delta": 0.9}))


def test_close_before_expiry_must_be_below_dte_min():
    assert_field_error(build(TRADE, **{"exit.close_before_expiry_days": 21}), "$", "dte_min")
    parse_signal(build(TRADE, **{"exit.close_before_expiry_days": 20}))


# -- exit rules ------------------------------------------------------------------------
def test_close_defaults_reduce_pct_100():
    assert parse_signal(build(CLOSE)).signal.reduce_pct == 100


def test_close_with_explicit_100_ok():
    assert parse_signal(build(CLOSE, reduce_pct=100)).signal.reduce_pct == 100


def test_close_with_partial_pct_rejected():
    assert_field_error(build(CLOSE, reduce_pct=50), "$", "reduce_pct must be 100")


def test_reduce_pct_100_rejected():
    assert_field_error(build(REDUCE, reduce_pct=100), "$", "use action 'close'")


@pytest.mark.parametrize("pct", [0, -5, 101, 150])
def test_reduce_pct_out_of_range(pct):
    assert "reduce_pct" in fields_of(build(REDUCE, reduce_pct=pct))


def test_reduce_without_pct_rejected():
    # default is 100, which a reduce may not use
    assert_field_error(build(REDUCE, reduce_pct=DROP), "$", "use action 'close'")


def test_reduce_pct_must_be_int():
    assert "reduce_pct" in fields_of(build(REDUCE, reduce_pct="50"))
    assert "reduce_pct" in fields_of(build(REDUCE, reduce_pct=50.5))


def test_exit_unknown_action():
    assert "action" in fields_of(build(CLOSE, action="flatten"))


def test_exit_does_not_accept_entry_keys():
    assert fields_of(build(CLOSE, underlying="NVDA")) == {"underlying": "unknown key"}


# -- versions --------------------------------------------------------------------------
@pytest.mark.parametrize("name", [TRADE, PUT, CLOSE, REDUCE])
def test_trade_and_exit_version_must_be_1(name):
    assert_field_error(build(name, version=2), "version", "version must be 1")


def test_rule_versions_may_increase():
    assert parse_signal(build(RULE, version=7)).signal.version == 7
    assert parse_signal(build(SCHEDULE)).signal.version == 2


@pytest.mark.parametrize("version", [0, -1, 1_000_001])
def test_version_bounds(version):
    assert "version" in fields_of(build(RULE, version=version))


# -- trigger / check_at ----------------------------------------------------------------
@pytest.mark.parametrize(
    "check_at", ["09:30 America/New_York", "00:00 America/New_York", "23:59 America/New_York"]
)
def test_check_at_valid(check_at):
    assert parse_signal(build(RULE, **{"trigger.check_at": check_at})).signal.trigger


@pytest.mark.parametrize(
    "check_at",
    [
        "24:00 America/New_York",
        "9:30 America/New_York",
        "09:60 America/New_York",
        "09:30",
        "09:30 UTC",
        "09:30 Europe/Berlin",
        "09:30 America/New_York ",
        "morning",
    ],
)
def test_check_at_invalid(check_at):
    assert "trigger.check_at" in fields_of(build(SCHEDULE, **{"trigger.check_at": check_at}))


def test_condition_trigger_requires_condition():
    assert_field_error(build(RULE, **{"trigger.condition": DROP}), "trigger", "condition")


def test_condition_trigger_without_check_at_ok():
    parse_signal(build(RULE, **{"trigger.check_at": DROP}))


def test_schedule_trigger_requires_check_at():
    assert_field_error(build(SCHEDULE, **{"trigger.check_at": DROP}), "trigger", "check_at")


def test_schedule_trigger_forbids_condition():
    assert_field_error(
        build(SCHEDULE, **{"trigger.condition": "close > 1"}), "trigger", "not allowed"
    )


def test_invalid_condition_reports_reason():
    assert_field_error(
        build(RULE, **{"trigger.condition": "close and 1"}), "trigger", "trigger.condition"
    )


@pytest.mark.parametrize("condition", ["close", "__import__('os')", "x" * 501])
def test_bad_conditions_rejected(condition):
    errors = fields_of(build(RULE, **{"trigger.condition": condition}))
    assert any(f.startswith("trigger") for f in errors)


def test_empty_condition_rejected():
    assert_field_error(build(RULE, **{"trigger.condition": ""}), "trigger", "required")


def test_unknown_trigger_type():
    assert fields_of(build(RULE, **{"trigger.type": "webhook"}))


def test_max_entries_per_day_bounds():
    assert "max_entries_per_day" in fields_of(build(RULE, max_entries_per_day=0))
    assert "max_entries_per_day" in fields_of(build(RULE, max_entries_per_day=11))


# -- timestamps ------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["created_at", "valid_until"])
@pytest.mark.parametrize("value", ["2026-10-19T13:40:00", "2026-10-19 13:40:00", "2026-10-19"])
def test_timezone_naive_timestamps_rejected(field, value):
    assert field in fields_of(build(TRADE, **{field: value}))


def test_timestamp_with_offset_accepted():
    sig = parse_signal(build(TRADE, created_at="2026-10-19T09:40:00-04:00")).signal
    assert sig.created_at.utcoffset().total_seconds() == -4 * 3600


def test_unquoted_yaml_timestamp_stays_strict():
    raw = build(TRADE).replace(
        "created_at: '2026-10-19T13:40:00Z'", "created_at: 2026-10-19T13:40:00"
    )
    assert "created_at: 2026-10-19T13:40:00\n" in raw
    assert "created_at" in fields_of(raw)


def test_unquoted_aware_timestamp_parses():
    raw = build(TRADE).replace(
        "created_at: '2026-10-19T13:40:00Z'", "created_at: 2026-10-19T13:40:00Z"
    )
    assert "created_at: 2026-10-19T13:40:00Z\n" in raw
    assert parse_signal(raw).signal.created_at.year == 2026


# -- YAML hardening and framing --------------------------------------------------------
def test_yaml_alias_rejected():
    raw = build(CLOSE).replace("target_signal_id: nvda-momo-2026-10-19-001", "target_signal_id: *x")
    raw = raw.replace("signal_id: nvda-momo-2026-10-19-001-exit", "signal_id: &x abc-exit-001")
    assert_field_error(raw, "$", "aliases")


def test_yaml_merge_key_rejected():
    raw = build(CLOSE).replace("action: close", "base: &b {x: 1}\n<<: *b\naction: close")
    assert "$" in fields_of(raw)


def test_duplicate_keys_rejected():
    raw = build(CLOSE).replace("action: close", "action: close\naction: close")
    assert_field_error(raw, "$", "duplicate key 'action'")


def test_duplicate_nested_keys_rejected():
    raw = build(TRADE).replace("  max_contracts: 2", "  max_contracts: 2\n  max_contracts: 99")
    assert_field_error(raw, "$", "duplicate key")


def test_yaml_python_tags_rejected():
    raw = build(CLOSE).replace("action: close", "action: !!python/object/apply:os.getcwd []")
    assert "$" in fields_of(raw)


def test_invalid_yaml_syntax():
    assert_field_error("---\nkey: [unclosed\n---\n", "$", "invalid YAML")


@pytest.mark.parametrize("front", ["- a\n- b", "just a string", "42", ""])
def test_non_mapping_front_matter(front):
    assert_field_error(f"---\n{front}\n---\nbody", "$", "must be a mapping")


def test_non_mapping_empty_front_matter():
    assert_field_error("---\n---\nbody", "$")


def test_nan_and_infinity_rejected():
    assert fields_of(build(TRADE).replace("confidence: 0.62", "confidence: .nan"))
    assert fields_of(build(TRADE).replace("confidence: 0.62", "confidence: .inf"))


def test_non_string_keys_rejected():
    assert fields_of("---\n1: a\n---\n")


def test_missing_closing_delimiter():
    raw = build(TRADE)
    cut = raw[: raw.index("\n---\n")]
    assert_field_error(cut, "$", "not closed")


def test_missing_opening_delimiter():
    assert_field_error("kind: trade\n", "$", "must start with '---'")
    assert_field_error("\n---\nkind: trade\n---\n", "$", "must start with '---'")
    assert_field_error("", "$", "must start with '---'")


def test_closing_delimiter_at_eof_without_newline():
    raw = build(CLOSE, body="")
    assert raw.endswith("---\n")
    parsed = parse_signal(raw.rstrip("\n"))
    assert parsed.body == ""


def test_bom_is_stripped():
    parsed = parse_signal("﻿" + build(TRADE))
    assert parsed.signal.signal_id == "nvda-momo-2026-10-19-001"


def test_crlf_line_endings():
    raw = build(TRADE).replace("\n", "\r\n")
    parsed = parse_signal(raw)
    assert parsed.signal.signal_id == "nvda-momo-2026-10-19-001"
    assert "\r" not in parsed.body


def test_bom_and_crlf_together():
    parse_signal("﻿" + build(RULE).replace("\n", "\r\n"))


def test_body_may_contain_horizontal_rules():
    body = "\ntext\n---\nmore\n---\nend\n"
    assert parse_signal(build(TRADE, body=body)).body == body


def test_split_front_matter():
    front, body = split_front_matter("---\na: 1\n---\nhello\n")
    assert front == "a: 1"
    assert body == "hello\n"


def test_body_with_yaml_like_content_is_inert():
    body = "\nkind: exit\nproducer: attacker\n"
    parsed = parse_signal(build(TRADE, body=body))
    assert parsed.signal.producer == "momentum-agent"
    assert parsed.body == body


def test_parsed_models_are_frozen():
    sig = parse_signal(build(TRADE)).signal
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic ValidationError
        sig.underlying = "AAPL"


def test_error_str_lists_fields():
    with pytest.raises(SignalParseError) as exc:
        parse_signal(build(TRADE, leverage=1))
    assert str(exc.value) == "leverage: unknown key"
    assert exc.value.errors[0].as_dict() == {"field": "leverage", "message": "unknown key"}


# -- JSON Schema -----------------------------------------------------------------------
def test_committed_schema_matches_render_schema():
    assert SCHEMA_FILE.read_text(encoding="utf-8") == render_schema()


def test_schema_is_valid_draft_2020_12():
    schema = json.loads(render_schema())
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize(
    "path", example_files("valid") + example_files("adversarial"), ids=lambda p: p.name
)
def test_valid_examples_validate_against_json_schema(path):
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text.split("---\n", 2)[1])
    validator = Draft202012Validator(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))
    errors = list(validator.iter_errors(json.loads(json.dumps(data))))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize(
    "overrides",
    [
        {"leverage": 3},
        {"strategy": "short_call"},
        {"order.type": "market"},
        {"contract.dte_min": "21"},
        {"confidence": 2},
        {"underlying": "nvda"},
    ],
)
def test_schema_rejects_what_the_parser_rejects(overrides):
    data = json.loads(json.dumps(front_matter(TRADE)))
    for key, value in overrides.items():
        target = data
        *parents, last = key.split(".")
        for part in parents:
            target = target[part]
        target[last] = value
    validator = Draft202012Validator(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))
    assert list(validator.iter_errors(data))
