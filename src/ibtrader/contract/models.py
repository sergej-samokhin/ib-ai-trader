"""Signal Contract v1.0 as strict pydantic models (source of the published JSON Schema)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ibtrader import expr

CONTRACT_VERSION = "1.0"

SignalId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")]
ProducerId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")]
Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,5}(\.[A-Z])?$")]
CheckAt = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d America/New_York$")]


class Strategy(StrEnum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"

    @property
    def right(self) -> str:
        return "C" if self in (Strategy.LONG_CALL, Strategy.BULL_CALL_SPREAD) else "P"

    @property
    def is_spread(self) -> bool:
        return self in (Strategy.BULL_CALL_SPREAD, Strategy.BEAR_PUT_SPREAD)

    @property
    def direction(self) -> str:
        return "bullish" if self.right == "C" else "bearish"


ALLOWED_STRATEGIES = frozenset(Strategy)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ContractSpec(_Strict):
    dte_min: int = Field(ge=0, le=365)
    dte_max: int = Field(ge=0, le=365)
    long_leg_delta: float = Field(ge=0.05, le=0.95)
    short_leg_delta: float | None = Field(None, ge=0.01, le=0.95)

    @model_validator(mode="after")
    def _window(self) -> ContractSpec:
        if self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        return self


class OrderSpec(_Strict):
    type: Literal["limit"]
    price: Literal["mid"]
    max_reprice_steps: int = Field(ge=0, le=10)


class SizingSpec(_Strict):
    max_premium_usd: float = Field(gt=0, le=100_000)
    max_contracts: int = Field(ge=1, le=100)


class ExitRules(_Strict):
    take_profit_pct: float = Field(gt=0, le=1000)
    stop_loss_pct: float = Field(gt=0, le=100)
    close_before_expiry_days: int = Field(ge=0, le=60)


class Envelope(_Strict):
    contract_version: Literal["1.0"]
    signal_id: SignalId
    version: int = Field(ge=1, le=1_000_000)
    producer: ProducerId
    created_at: AwareDatetime
    valid_until: AwareDatetime
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ttl(self) -> Envelope:
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be after created_at")
        return self


class _EntrySignal(Envelope):
    underlying: Ticker
    strategy: Strategy
    contract: ContractSpec
    order: OrderSpec
    sizing: SizingSpec
    exit: ExitRules

    @model_validator(mode="after")
    def _legs(self) -> _EntrySignal:
        short = self.contract.short_leg_delta
        if self.strategy.is_spread:
            if short is None:
                raise ValueError(f"contract.short_leg_delta is required for {self.strategy}")
            if short >= self.contract.long_leg_delta:
                raise ValueError("contract.short_leg_delta must be smaller than long_leg_delta")
        elif short is not None:
            raise ValueError(f"contract.short_leg_delta is not allowed for {self.strategy}")
        if self.exit.close_before_expiry_days >= self.contract.dte_min and self.contract.dte_min:
            raise ValueError("exit.close_before_expiry_days must be smaller than contract.dte_min")
        return self


class TradeSignal(_EntrySignal):
    kind: Literal["trade"]

    @field_validator("version")
    @classmethod
    def _immutable(cls, v: int) -> int:
        if v != 1:
            raise ValueError("trade signals are immutable; version must be 1")
        return v


class Trigger(_Strict):
    type: Literal["condition", "schedule"]
    condition: str | None = Field(None, max_length=expr.MAX_LENGTH)
    check_at: CheckAt | None = None

    @model_validator(mode="after")
    def _shape(self) -> Trigger:
        if self.type == "condition":
            if not self.condition:
                raise ValueError("trigger.condition is required for type 'condition'")
            try:
                expr.parse(self.condition)
            except expr.ExpressionError as exc:
                raise ValueError(f"trigger.condition: {exc}") from None
        else:
            if self.condition is not None:
                raise ValueError("trigger.condition is not allowed for type 'schedule'")
            if self.check_at is None:
                raise ValueError("trigger.check_at is required for type 'schedule'")
        return self


class RuleSignal(_EntrySignal):
    kind: Literal["rule"]
    trigger: Trigger
    max_entries_per_day: int = Field(ge=1, le=10)


class ExitSignal(Envelope):
    kind: Literal["exit"]
    target_signal_id: SignalId
    action: Literal["close", "reduce"]
    reduce_pct: int = Field(100, ge=1, le=100)

    @field_validator("version")
    @classmethod
    def _immutable(cls, v: int) -> int:
        if v != 1:
            raise ValueError("exit signals are immutable; version must be 1")
        return v

    @model_validator(mode="after")
    def _pct(self) -> ExitSignal:
        if self.action == "close" and self.reduce_pct != 100:
            raise ValueError("reduce_pct must be 100 (or omitted) for action 'close'")
        if self.action == "reduce" and self.reduce_pct == 100:
            raise ValueError("use action 'close' to exit 100%")
        return self


Signal = Annotated[TradeSignal | RuleSignal | ExitSignal, Field(discriminator="kind")]
EntrySignal = TradeSignal | RuleSignal
