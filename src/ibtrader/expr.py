"""Whitelisted condition language for `rule` signals.

Grammar (no function calls, no attribute access, no strings, nothing executable)::

    expr       := or_expr
    or_expr    := and_expr ("or" and_expr)*
    and_expr   := not_expr ("and" not_expr)*
    not_expr   := "not" not_expr | comparison
    comparison := arith (CMP arith)?
    arith      := term (("+" | "-") term)*
    term       := factor (("*" | "/") factor)*
    factor     := NUMBER | INDICATOR | "(" expr ")" | "-" factor

Indicators: close, open, high, low, volume, prev_close, sma_N, ema_N, rsi_N, avg_volume_N
with 2 <= N <= 200. The whole expression must be boolean.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

MAX_LENGTH = 500
MAX_TOKENS = 200
MAX_DEPTH = 20
MAX_PERIOD = 200

BASE_INDICATORS = frozenset({"close", "open", "high", "low", "volume", "prev_close"})
PERIOD_INDICATORS = frozenset({"sma", "ema", "rsi", "avg_volume"})
_PERIOD_RE = re.compile(r"^(sma|ema|rsi|avg_volume)_([0-9]{1,3})$")
_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>[0-9]+(?:\.[0-9]+)?)|(?P<name>[a-z][a-z0-9_]*)|(?P<op><=|>=|==|!=|<|>|[-+*/()]))"
)
_CMP_OPS = {"<", "<=", ">", ">=", "==", "!="}
_KEYWORDS = {"and", "or", "not"}


class ExpressionError(ValueError):
    pass


class EvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Neg:
    operand: Node


@dataclass(frozen=True)
class BinOp:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Compare:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class BoolOp:
    op: str  # "and" | "or"
    left: Node
    right: Node


@dataclass(frozen=True)
class Not:
    operand: Node


Node = Num | Var | Neg | BinOp | Compare | BoolOp | Not


def validate_indicator(name: str) -> None:
    if name in BASE_INDICATORS:
        return
    match = _PERIOD_RE.match(name)
    if not match:
        raise ExpressionError(f"unknown indicator '{name}'")
    period = int(match.group(2))
    if not 2 <= period <= MAX_PERIOD:
        raise ExpressionError(f"indicator period out of range in '{name}' (2..{MAX_PERIOD})")


def indicator_period(name: str) -> int:
    match = _PERIOD_RE.match(name)
    return int(match.group(2)) if match else 1


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match or match.end() == pos:
            raise ExpressionError(f"unexpected character at position {pos}: {text[pos]!r}")
        pos = match.end()
        if match.group("num") is not None:
            tokens.append(("num", match.group("num")))
        elif match.group("name") is not None:
            name = match.group("name")
            tokens.append(("kw" if name in _KEYWORDS else "name", name))
        else:
            tokens.append(("op", match.group("op")))
        if len(tokens) > MAX_TOKENS:
            raise ExpressionError(f"expression has more than {MAX_TOKENS} tokens")
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.i = 0
        self.depth = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def accept(self, kind: str, value: str | None = None) -> bool:
        tok = self.peek()
        if tok and tok[0] == kind and (value is None or tok[1] == value):
            self.i += 1
            return True
        return False

    def enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError(f"expression nested deeper than {MAX_DEPTH}")

    def parse(self) -> Node:
        node = self.or_expr()
        if self.peek() is not None:
            raise ExpressionError(f"unexpected token '{self.peek()[1]}'")
        return node

    def or_expr(self) -> Node:
        node = self.and_expr()
        while self.accept("kw", "or"):
            node = BoolOp("or", node, self.and_expr())
        return node

    def and_expr(self) -> Node:
        node = self.not_expr()
        while self.accept("kw", "and"):
            node = BoolOp("and", node, self.not_expr())
        return node

    def not_expr(self) -> Node:
        if self.accept("kw", "not"):
            self.enter()
            node = Not(self.not_expr())
            self.depth -= 1
            return node
        return self.comparison()

    def comparison(self) -> Node:
        left = self.arith()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in _CMP_OPS:
            self.i += 1
            return Compare(tok[1], left, self.arith())
        return left

    def arith(self) -> Node:
        node = self.term()
        while True:
            tok = self.peek()
            if tok and tok[0] == "op" and tok[1] in "+-":
                self.i += 1
                node = BinOp(tok[1], node, self.term())
            else:
                return node

    def term(self) -> Node:
        node = self.factor()
        while True:
            tok = self.peek()
            if tok and tok[0] == "op" and tok[1] in "*/":
                self.i += 1
                node = BinOp(tok[1], node, self.factor())
            else:
                return node

    def factor(self) -> Node:
        tok = self.peek()
        if tok is None:
            raise ExpressionError("unexpected end of expression")
        kind, value = tok
        if kind == "num":
            self.i += 1
            return Num(float(value))
        if kind == "name":
            self.i += 1
            validate_indicator(value)
            return Var(value)
        if kind == "op" and value == "(":
            self.i += 1
            self.enter()
            node = self.or_expr()
            self.depth -= 1
            if not self.accept("op", ")"):
                raise ExpressionError("missing closing parenthesis")
            return node
        if kind == "op" and value == "-":
            self.i += 1
            self.enter()
            node = Neg(self.factor())
            self.depth -= 1
            return node
        raise ExpressionError(f"unexpected token '{value}'")


def _type_of(node: Node) -> str:
    """Static type check: returns 'bool' or 'num'; raises on mixing."""
    match node:
        case Num() | Var():
            return "num"
        case Neg(operand):
            if _type_of(operand) != "num":
                raise ExpressionError("unary minus applied to a boolean")
            return "num"
        case Not(operand):
            if _type_of(operand) != "bool":
                raise ExpressionError("'not' applied to a number")
            return "bool"
        case BinOp(_, left, right):
            if _type_of(left) != "num" or _type_of(right) != "num":
                raise ExpressionError("arithmetic on a boolean")
            return "num"
        case Compare(_, left, right):
            if _type_of(left) != "num" or _type_of(right) != "num":
                raise ExpressionError("comparison of a boolean")
            return "bool"
        case BoolOp(op, left, right):
            if _type_of(left) != "bool" or _type_of(right) != "bool":
                raise ExpressionError(f"'{op}' requires comparisons on both sides")
            return "bool"
    raise ExpressionError("invalid expression")  # pragma: no cover


def parse(text: str) -> Node:
    if not isinstance(text, str) or not text.strip():
        raise ExpressionError("condition is empty")
    if len(text) > MAX_LENGTH:
        raise ExpressionError(f"condition longer than {MAX_LENGTH} characters")
    tree = _Parser(_tokenize(text)).parse()
    if _type_of(tree) != "bool":
        raise ExpressionError("condition must evaluate to true/false (use a comparison)")
    return tree


def indicators_used(node: Node) -> set[str]:
    match node:
        case Var(name):
            return {name}
        case Num():
            return set()
        case Neg(operand) | Not(operand):
            return indicators_used(operand)
        case BinOp(_, left, right) | Compare(_, left, right) | BoolOp(_, left, right):
            return indicators_used(left) | indicators_used(right)
    return set()  # pragma: no cover


def evaluate(node: Node, values: Mapping[str, float]) -> bool | float:
    match node:
        case Num(value):
            return value
        case Var(name):
            if name not in values or values[name] is None:
                raise EvaluationError(f"no value for indicator '{name}'")
            return float(values[name])
        case Neg(operand):
            return -evaluate(operand, values)
        case Not(operand):
            return not evaluate(operand, values)
        case BinOp(op, left, right):
            lv, rv = evaluate(left, values), evaluate(right, values)
            if op == "+":
                return lv + rv
            if op == "-":
                return lv - rv
            if op == "*":
                return lv * rv
            if rv == 0:
                raise EvaluationError("division by zero")
            return lv / rv
        case Compare(op, left, right):
            lv, rv = evaluate(left, values), evaluate(right, values)
            return {
                "<": lv < rv,
                "<=": lv <= rv,
                ">": lv > rv,
                ">=": lv >= rv,
                "==": lv == rv,
                "!=": lv != rv,
            }[op]
        case BoolOp("and", left, right):
            return bool(evaluate(left, values)) and bool(evaluate(right, values))
        case BoolOp(_, left, right):
            return bool(evaluate(left, values)) or bool(evaluate(right, values))
    raise EvaluationError("invalid expression")  # pragma: no cover


def evaluate_condition(text: str, values: Mapping[str, float]) -> bool:
    return bool(evaluate(parse(text), values))
