from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Offset(str, Enum):
    AUTO = "AUTO"
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSE_TODAY = "CLOSE_TODAY"
    CLOSE_YESTERDAY = "CLOSE_YESTERDAY"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tick:
    symbol: str
    timestamp: datetime
    last_price: float
    volume: float = 0.0
    turnover: float = 0.0
    open_interest: float = 0.0
    bid_price_1: float | None = None
    bid_volume_1: float = 0.0
    ask_price_1: float | None = None
    ask_volume_1: float = 0.0
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    pre_close_price: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    submitted_at: datetime
    order_type: OrderType = OrderType.MARKET
    offset: Offset = Offset.AUTO
    limit_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_at: datetime | None = None
    fill_price: float | None = None
    commission: float = 0.0
    reject_reason: str | None = None


@dataclass
class Trade:
    trade_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    commission: float
    timestamp: datetime
    offset: Offset = Offset.AUTO
    notional: float = 0.0
    margin: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def apply_fill(self, side: Side, quantity: float, price: float) -> float:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        signed_qty = quantity if side == Side.BUY else -quantity
        old_qty = self.quantity
        old_avg = self.avg_price
        realized = 0.0

        if old_qty == 0 or old_qty * signed_qty > 0:
            new_qty = old_qty + signed_qty
            self.avg_price = (
                (abs(old_qty) * old_avg + abs(signed_qty) * price) / abs(new_qty)
            )
            self.quantity = new_qty
            return realized

        closing_qty = min(abs(old_qty), abs(signed_qty))
        if old_qty > 0:
            realized = (price - old_avg) * closing_qty
        else:
            realized = (old_avg - price) * closing_qty

        new_qty = old_qty + signed_qty
        self.quantity = new_qty
        self.realized_pnl += realized

        if new_qty == 0:
            self.avg_price = 0.0
        elif old_qty * new_qty < 0:
            self.avg_price = price

        return realized

    def market_value(self, last_price: float) -> float:
        return self.quantity * last_price

    def unrealized_pnl(self, last_price: float) -> float:
        if self.quantity > 0:
            return (last_price - self.avg_price) * self.quantity
        if self.quantity < 0:
            return (self.avg_price - last_price) * abs(self.quantity)
        return 0.0
