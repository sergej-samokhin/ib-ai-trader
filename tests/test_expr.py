"""Condition language (expr) and indicator calculations."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ibtrader import expr, indicators
from ibtrader.domain import Bar
from ibtrader.expr import EvaluationError, ExpressionError

VALUES = {"close": 100.0, "open": 98.0, "sma_20": 95.0, "rsi_14": 55.0, "volume": 1000.0}


def ev(text: str, values=VALUES) -> bool:
    return expr.evaluate_condition(text, values)


# -- grammar and evaluation ------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("close > sma_20", True),
        ("close < sma_20", False),
        ("close >= 100", True),
        ("close <= 99.99", False),
        ("close == 100", True),
        ("close != 100", False),
        ("close > sma_20 and rsi_14 < 70", True),
        ("close > sma_20 and rsi_14 > 70", False),
        ("close < sma_20 or rsi_14 < 70", True),
        ("close < sma_20 or rsi_14 > 70", False),
        ("not close < sma_20", True),
        ("not not close > 1", True),
        ("not (close > 1 and rsi_14 > 99)", True),
        ("  close   >   1  ", True),
        ("close > 1\n", True),
        ("close>1", True),
        ("close*2 == 200", True),
        ("close/4 == 25", True),
        ("close + 1 > 100", True),
        ("close - 1 < 100", True),
        ("-close < 0", True),
        ("--close == 100", True),
        ("close - -1 == 101", True),
        ("1 < 2", True),
        ("2.5 * 2 == 5", True),
    ],
)
def test_evaluation(text, expected):
    assert ev(text) is expected


def test_arithmetic_precedence():
    assert ev("2 + 3 * 4 == 14")
    assert ev("(2 + 3) * 4 == 20")
    assert ev("10 - 4 - 3 == 3")  # left associative
    assert ev("100 / 10 / 5 == 2")
    assert ev("-2 * 3 == -6")
    assert ev("2 * -3 == -6")


def test_boolean_precedence_and_binds_tighter_than_or():
    # a or b and c  ==  a or (b and c)
    assert ev("1 > 0 or 1 > 2 and 1 > 2")
    assert not ev("(1 > 0 or 1 > 2) and 1 > 2")
    # not binds tighter than and
    assert ev("not 1 > 2 and 1 > 0")


def test_comparison_not_chainable():
    with pytest.raises(ExpressionError):
        expr.parse("1 < 2 < 3")


def test_or_short_circuit_does_not_hide_division_by_zero_in_type_check():
    # evaluation is short-circuited: right side not touched when left decides
    assert ev("close > 1 or close / 0 > 1")


def test_ast_shape():
    tree = expr.parse("close > sma_20 and rsi_14 < 70")
    assert isinstance(tree, expr.BoolOp)
    assert tree.op == "and"
    assert expr.indicators_used(tree) == {"close", "sma_20", "rsi_14"}


def test_indicators_used_excludes_numbers():
    assert expr.indicators_used(expr.parse("not (volume > avg_volume_10 * 2)")) == {
        "volume",
        "avg_volume_10",
    }


@pytest.mark.parametrize(
    "name", ["close", "open", "high", "low", "volume", "prev_close", "sma_2", "ema_200", "rsi_14"]
)
def test_known_indicators(name):
    expr.validate_indicator(name)
    assert expr.parse(f"{name} > 0")


def test_indicator_period():
    assert expr.indicator_period("sma_20") == 20
    assert expr.indicator_period("avg_volume_50") == 50
    assert expr.indicator_period("close") == 1


# -- type errors -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "close",  # not boolean
        "close + 1",
        "1",
        "close and 1",
        "1 and close > 1",
        "close > 1 and 1",
        "close or rsi_14",
        "not close",
        "not 1",
        "close + (rsi_14 > 3)",
        "(rsi_14 > 3) + close",
        "(close > 1) * 2 > 1",
        "-(close > 1) > 0",
        "(close > 1) > 0",
        "(close > 1) == (close > 2)",
        "close > (close > 1)",
        "(close > 1)",  # still boolean: fine -> see test below
    ][:-1],
)
def test_type_errors(text):
    with pytest.raises(ExpressionError):
        expr.parse(text)


def test_parenthesised_boolean_is_fine():
    assert expr.parse("(close > 1)")


# -- unknown indicators and period bounds ----------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "foo > 1",
        "closes > 1",
        "CLOSE > 1",
        "sma > 1",
        "sma_ > 1",
        "sma_x > 1",
        "wma_10 > 1",
        "sma_1 > 1",
        "sma_0 > 1",
        "sma_201 > 1",
        "ema_999 > 1",
        "rsi_1000 > 1",  # regex allows 3 digits only
        "rsi_5000 > 1",
        "avg_volume_1 > 1",
        "close_5 > 1",
        "sma_20_ > 1",
        "sma_020 > 1" if False else "sma_-5 > 1",
    ],
)
def test_unknown_or_out_of_range_indicators(text):
    with pytest.raises(ExpressionError):
        expr.parse(text)


@pytest.mark.parametrize("name", ["sma_2", "sma_200", "rsi_2", "avg_volume_200"])
def test_period_bounds_inclusive(name):
    expr.parse(f"{name} > 0")


def test_period_with_leading_zero_still_in_range():
    # documented behaviour of the current grammar: leading zeros parse as the number
    assert expr.parse("sma_020 > 0")


# -- limits ----------------------------------------------------------------------------
def test_empty_and_blank():
    for text in ("", "   ", "\n\t"):
        with pytest.raises(ExpressionError, match="empty"):
            expr.parse(text)


def test_non_string_input():
    with pytest.raises(ExpressionError):
        expr.parse(None)  # type: ignore[arg-type]
    with pytest.raises(ExpressionError):
        expr.parse(5)  # type: ignore[arg-type]


def test_length_limit():
    ok = "close > 1" + " " * (expr.MAX_LENGTH - len("close > 1"))
    assert len(ok) == expr.MAX_LENGTH
    expr.parse(ok)
    with pytest.raises(ExpressionError, match="longer than"):
        expr.parse(ok + " ")


def test_token_limit():
    ok = " and ".join(["close > 1"] * 20)  # 20*3 + 19 = 79 tokens
    expr.parse(ok)
    many = " + ".join(["1"] * 120) + " > 0"  # 120 + 119 + 2 = 241 tokens, < 500 chars
    assert len(many) <= expr.MAX_LENGTH
    with pytest.raises(ExpressionError, match="tokens"):
        expr.parse(many)


def test_depth_limit_parentheses():
    ok = "(" * expr.MAX_DEPTH + "close > 1" + ")" * expr.MAX_DEPTH
    expr.parse(ok)
    too_deep = "(" * (expr.MAX_DEPTH + 1) + "close > 1" + ")" * (expr.MAX_DEPTH + 1)
    with pytest.raises(ExpressionError, match="nested"):
        expr.parse(too_deep)


def test_depth_limit_not_chain():
    with pytest.raises(ExpressionError, match="nested"):
        expr.parse("not " * (expr.MAX_DEPTH + 1) + "close > 1")


def test_depth_limit_unary_minus_chain():
    with pytest.raises(ExpressionError, match="nested"):
        expr.parse("-" * (expr.MAX_DEPTH + 1) + "close > 1")


def test_no_recursion_error_on_deep_input():
    # 150 nested parens fit in the length limit and must fail cleanly, not blow the stack
    with pytest.raises(ExpressionError):
        expr.parse("(" * 240 + "close > 1" + ")" * 240)


@pytest.mark.parametrize("text", ["(close > 1", "close > 1)", "()", "close >", "> 1", "close 1"])
def test_syntax_errors(text):
    with pytest.raises(ExpressionError):
        expr.parse(text)


# -- malicious inputs ------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "__import__('os')",
        "__import__('os').system('id') > 1",
        "close.__class__",
        "close.__class__ > 1",
        "close .real > 1",
        "eval(1)",
        "eval(1) > 1",
        "exec('x') > 1",
        "open('/etc/passwd') > 1",
        "close > 'a'",
        'close > "a"',
        "'a' == 'a'",
        "lambda: 1",
        "(lambda x: x)(1) > 0",
        "close > 1; import os",
        "close > 1;",
        "close > 1 # comment",
        "close > 1\nand close < 5 and __import__",
        "[1,2][0] > 0",
        "{1:2} == 1",
        "close[0] > 1",
        "close > 1 if 1 else 2",
        "close ** 2 > 1",
        "close // 2 > 1",
        "close % 2 > 1",
        "close & 1 > 1",
        "close | 1 > 1",
        "close ^ 1 > 1",
        "~close > 1",
        "close = 1",
        "close := 1",
        "close, 1",
        "`id`",
        "$(id)",
        "close > 1e5",
        "close > 0x10",
        "close > 1_000",
        "close > .5",
        "True",
        "None == None",
        "close is close",
        "close in close",
        "globals()",
        "__builtins__ > 1",
        "close > 1 \x00",
        "close > ٣",  # non-ASCII digit
        "clоse > 1",  # cyrillic o
    ],
)
def test_malicious_inputs_raise_expression_error(text):
    with pytest.raises(ExpressionError):
        expr.parse(text)


def test_evaluate_condition_never_executes_input(tmp_path):
    marker = tmp_path / "pwned"
    with pytest.raises(ExpressionError):
        expr.evaluate_condition(f"__import__('os').system('touch {marker}')", {})
    assert not marker.exists()


# -- evaluation errors -----------------------------------------------------------------
def test_division_by_zero():
    with pytest.raises(EvaluationError, match="division by zero"):
        ev("close / 0 > 1")


def test_division_by_computed_zero():
    with pytest.raises(EvaluationError, match="division"):
        ev("close / (close - 100) > 1")


def test_division_by_zero_indicator_value():
    with pytest.raises(EvaluationError):
        expr.evaluate_condition("close / volume > 1", {"close": 1.0, "volume": 0.0})


def test_missing_value():
    with pytest.raises(EvaluationError, match="sma_50"):
        ev("close > sma_50")


def test_none_value_is_missing():
    with pytest.raises(EvaluationError):
        expr.evaluate_condition("close > sma_20", {"close": 1.0, "sma_20": None})


def test_errors_are_value_errors_but_distinct():
    assert issubclass(ExpressionError, ValueError)
    assert issubclass(EvaluationError, ValueError)
    assert not issubclass(EvaluationError, ExpressionError)


# -- indicators ------------------------------------------------------------------------
def test_sma():
    assert indicators.sma([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)
    assert indicators.sma([1, 2, 3], 3) == pytest.approx(2.0)
    assert indicators.sma([1, 2], 3) is None
    assert indicators.sma([], 2) is None


def test_ema_hand_computed():
    # period 3: k = 0.5, seed = mean(1,2,3) = 2 -> 4*0.5+2*0.5 = 3 -> 5*0.5+3*0.5 = 4
    assert indicators.ema([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)
    # exactly `period` values: seed only
    assert indicators.ema([1, 2, 3], 3) == pytest.approx(2.0)
    assert indicators.ema([1, 2], 3) is None


def test_ema_period_2_hand_computed():
    # k = 2/3; seed 1.5; 3 -> 3*2/3 + 1.5/3 = 2.5; 4 -> 4*2/3 + 2.5/3 = 3.5
    assert indicators.ema([1, 2, 3, 4], 2) == pytest.approx(3.5)


def test_rsi_all_gains_is_100():
    assert indicators.rsi([float(i) for i in range(1, 20)], 14) == 100.0


def test_rsi_all_losses_is_0():
    assert indicators.rsi([float(i) for i in range(20, 1, -1)], 14) == pytest.approx(0.0)


def test_rsi_flat_series_is_100():
    # no losses -> avg_loss == 0 -> 100 by the implementation's convention
    assert indicators.rsi([5.0] * 20, 14) == 100.0


def test_rsi_hand_computed_seed_only():
    # period 2, closes 1,2,1,2: changes +1,-1,+1 -> seed gains (1,0) losses (0,1)
    # avg_gain=.5 avg_loss=.5 ; then change +1: gain=(.5*1+1)/2=.75 loss=(.5*1+0)/2=.25
    # RS=3 -> RSI = 100-100/4 = 75
    assert indicators.rsi([1, 2, 1, 2], 2) == pytest.approx(75.0)


def test_rsi_equal_gains_and_losses_is_50():
    assert indicators.rsi([1, 2, 1], 2) == pytest.approx(50.0)


def test_rsi_needs_period_plus_one_values():
    assert indicators.rsi([1.0, 2.0], 2) is None
    assert indicators.rsi([1.0, 2.0, 3.0], 2) == 100.0


def bars(closes, volumes=None, start=date(2026, 1, 5)):
    volumes = volumes or [1000.0] * len(closes)
    return [
        Bar(start + timedelta(days=i), c - 1, c + 2, c - 2, c, v)
        for i, (c, v) in enumerate(zip(closes, volumes, strict=True))
    ]


def test_compute_base_fields():
    out = indicators.compute(
        ["close", "open", "high", "low", "volume", "prev_close"], bars([10, 12], [5, 7])
    )
    assert out == {
        "close": 12,
        "open": 11,
        "high": 14,
        "low": 10,
        "volume": 7,
        "prev_close": 10,
    }


def test_compute_prev_close_needs_two_bars():
    assert indicators.compute(["prev_close"], bars([10])) == {"prev_close": None}


def test_compute_period_indicators():
    data = bars([1, 2, 3, 4, 5], [10, 20, 30, 40, 50])
    out = indicators.compute(["sma_3", "ema_3", "rsi_2", "avg_volume_2"], data)
    assert out["sma_3"] == pytest.approx(4.0)
    assert out["ema_3"] == pytest.approx(4.0)
    assert out["rsi_2"] == 100.0
    assert out["avg_volume_2"] == pytest.approx(45.0)


def test_compute_with_too_few_bars_returns_none():
    data = bars([1, 2, 3])
    out = indicators.compute(["sma_20", "ema_20", "rsi_14", "avg_volume_20"], data)
    assert out == {"sma_20": None, "ema_20": None, "rsi_14": None, "avg_volume_20": None}


def test_compute_no_bars():
    assert indicators.compute(["close", "sma_5"], []) == {"close": None, "sma_5": None}


def test_compute_none_values_make_evaluation_fail():
    values = indicators.compute(["close", "sma_20"], bars([1, 2, 3]))
    with pytest.raises(EvaluationError):
        expr.evaluate_condition("close > sma_20", values)


def test_compute_end_to_end_condition():
    closes = [float(c) for c in range(1, 41)]
    names = expr.indicators_used(expr.parse("close > sma_20 and rsi_14 > 70"))
    assert expr.evaluate_condition(
        "close > sma_20 and rsi_14 > 70", indicators.compute(names, bars(closes))
    )


def test_bars_needed():
    assert indicators.bars_needed(["close"]) == 30
    assert indicators.bars_needed(["sma_20"]) == 60
    assert indicators.bars_needed(["sma_5", "ema_200"]) == 600
    assert indicators.bars_needed([]) == 30
