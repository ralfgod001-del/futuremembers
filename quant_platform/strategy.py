from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from .models import Bar, Offset, Order, OrderType, Position, Side, Tick, Trade


class EnginePort(Protocol):
    current_time: datetime | None

    def submit_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        offset: Offset = Offset.AUTO,
    ) -> Order:
        ...

    def history(self, symbol: str, limit: int | None = None) -> list[Bar]:
        ...

    def position(self, symbol: str) -> Position:
        ...

    def last_price(self, symbol: str) -> float | None:
        ...

    def last_tick(self, symbol: str) -> Tick | None:
        ...


@dataclass
class StrategyContext:
    engine: EnginePort

    @property
    def now(self) -> datetime | None:
        return self.engine.current_time

    def buy(
        self,
        symbol: str,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        offset: Offset = Offset.AUTO,
    ) -> Order:
        return self.engine.submit_order(symbol, Side.BUY, quantity, order_type, limit_price, offset)

    def sell(
        self,
        symbol: str,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        offset: Offset = Offset.AUTO,
    ) -> Order:
        return self.engine.submit_order(symbol, Side.SELL, quantity, order_type, limit_price, offset)

    def target_position(self, symbol: str, target_quantity: float) -> Order | None:
        current_quantity = self.position(symbol).quantity
        delta = target_quantity - current_quantity
        if abs(delta) < 1e-12:
            return None
        if delta > 0:
            return self.buy(symbol, delta)
        return self.sell(symbol, abs(delta))

    def history(self, symbol: str, limit: int | None = None) -> list[Bar]:
        return self.engine.history(symbol, limit)

    def closes(self, symbol: str, limit: int | None = None) -> list[float]:
        return [bar.close for bar in self.history(symbol, limit)]

    def position(self, symbol: str) -> Position:
        return self.engine.position(symbol)

    def last_price(self, symbol: str) -> float | None:
        return self.engine.last_price(symbol)

    def last_tick(self, symbol: str) -> Tick | None:
        return self.engine.last_tick(symbol)


class Strategy:
    name = "strategy"
    state_schema_version = 1

    def on_init(self, context: StrategyContext) -> None:
        pass

    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        pass

    def on_tick(self, context: StrategyContext, tick: Tick) -> None:
        pass

    def on_order(self, context: StrategyContext, order: Order) -> None:
        pass

    def on_trade(self, context: StrategyContext, trade: Trade) -> None:
        pass

    def on_finish(self, context: StrategyContext) -> None:
        pass

    def snapshot_state(self) -> Mapping[str, Any]:
        return {}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        pass

    def migrate_state(
        self,
        state: Mapping[str, Any],
        from_version: int,
    ) -> Mapping[str, Any]:
        if from_version > self.state_schema_version:
            raise ValueError(
                f"cannot load newer strategy state version {from_version} "
                f"with strategy version {self.state_schema_version}"
            )
        return state
