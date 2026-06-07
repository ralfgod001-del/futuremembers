from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .models import Bar, Offset, Tick
from .strategy import Strategy, StrategyContext


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


@dataclass
class MovingAverageCrossStrategy(Strategy):
    fast_window: int = 8
    slow_window: int = 24
    quantity: float = 1.0
    allow_short: bool = False
    symbol: str | None = None

    name = "moving_average_cross"

    def on_init(self, context: StrategyContext) -> None:
        if self.fast_window <= 0 or self.slow_window <= 0:
            raise ValueError("moving average windows must be positive")
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")

    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        symbol = self.symbol or bar.symbol
        if bar.symbol != symbol:
            return

        closes = context.closes(symbol, self.slow_window + 1)
        if len(closes) < self.slow_window + 1:
            return

        fast_prev = mean(closes[-self.fast_window - 1 : -1])
        slow_prev = mean(closes[-self.slow_window - 1 : -1])
        fast_now = mean(closes[-self.fast_window :])
        slow_now = mean(closes[-self.slow_window :])

        if fast_prev <= slow_prev and fast_now > slow_now:
            context.target_position(symbol, self.quantity)
        elif fast_prev >= slow_prev and fast_now < slow_now:
            target = -self.quantity if self.allow_short else 0.0
            context.target_position(symbol, target)


@dataclass
class BuyFirstTickStrategy(Strategy):
    quantity: float = 1.0
    symbol: str | None = None
    offset: Offset = Offset.OPEN
    has_submitted: bool = False

    name = "buy_first_tick"
    state_schema_version = 1

    def __post_init__(self) -> None:
        if isinstance(self.offset, str):
            self.offset = Offset[self.offset.upper().replace("-", "_")]

    def on_init(self, context: StrategyContext) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")

    def on_tick(self, context: StrategyContext, tick: Tick) -> None:
        symbol = self.symbol or tick.symbol
        if tick.symbol != symbol:
            return
        if not self.has_submitted and context.position(symbol).quantity == 0:
            context.buy(symbol, self.quantity, offset=self.offset)
            self.has_submitted = True

    def snapshot_state(self) -> Mapping[str, Any]:
        return {"has_submitted": self.has_submitted}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self.has_submitted = bool(state.get("has_submitted", False))
