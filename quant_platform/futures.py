from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Offset, Position, Side


@dataclass(frozen=True)
class CommissionRule:
    rate: float = 0.0
    per_contract: float = 0.0
    min_commission: float = 0.0
    close_today_rate: float | None = None
    close_today_per_contract: float | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "CommissionRule":
        if not raw:
            return cls()
        return cls(
            rate=float(raw.get("rate", raw.get("commission_rate", 0.0))),
            per_contract=float(raw.get("per_contract", 0.0)),
            min_commission=float(raw.get("min_commission", 0.0)),
            close_today_rate=_optional_float(raw.get("close_today_rate")),
            close_today_per_contract=_optional_float(raw.get("close_today_per_contract")),
        )

    def calculate(self, notional: float, quantity: float, offset: Offset) -> float:
        rate = self.rate
        per_contract = self.per_contract
        if offset == Offset.CLOSE_TODAY:
            rate = self.close_today_rate if self.close_today_rate is not None else rate
            per_contract = (
                self.close_today_per_contract
                if self.close_today_per_contract is not None
                else per_contract
            )
        commission = abs(notional) * rate + abs(quantity) * per_contract
        return max(commission, self.min_commission)

    def calculate_breakdown(
        self,
        price: float,
        multiplier: float,
        opened_quantity: float = 0.0,
        closed_today_quantity: float = 0.0,
        closed_yesterday_quantity: float = 0.0,
    ) -> float:
        default_quantity = opened_quantity + closed_yesterday_quantity
        default_notional = abs(price * multiplier * default_quantity)
        close_today_rate = (
            self.close_today_rate if self.close_today_rate is not None else self.rate
        )
        close_today_per_contract = (
            self.close_today_per_contract
            if self.close_today_per_contract is not None
            else self.per_contract
        )
        close_today_notional = abs(price * multiplier * closed_today_quantity)
        commission = (
            default_notional * self.rate
            + abs(default_quantity) * self.per_contract
            + close_today_notional * close_today_rate
            + abs(closed_today_quantity) * close_today_per_contract
        )
        return max(commission, self.min_commission)


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    exchange: str = ""
    multiplier: float = 1.0
    tick_size: float = 0.01
    margin_rate: float = 0.0
    commission: CommissionRule = field(default_factory=CommissionRule)

    @classmethod
    def from_mapping(cls, symbol: str, raw: dict[str, Any]) -> "ContractSpec":
        return cls(
            symbol=symbol,
            exchange=str(raw.get("exchange", "")),
            multiplier=float(raw.get("multiplier", 1.0)),
            tick_size=float(raw.get("tick_size", 0.01)),
            margin_rate=float(raw.get("margin_rate", 0.0)),
            commission=CommissionRule.from_mapping(raw.get("commission")),
        )

    def notional(self, price: float, quantity: float) -> float:
        return price * quantity * self.multiplier

    def margin(self, price: float, quantity: float) -> float:
        return abs(self.notional(price, quantity)) * self.margin_rate


@dataclass
class ContractRegistry:
    default: ContractSpec = field(default_factory=lambda: ContractSpec(symbol="*"))
    contracts: dict[str, ContractSpec] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "ContractRegistry":
        if not raw:
            return cls()
        default_raw = raw.get("default", {})
        default = ContractSpec.from_mapping("*", default_raw)
        contracts = {
            symbol: ContractSpec.from_mapping(symbol, item)
            for symbol, item in raw.items()
            if symbol != "default"
        }
        return cls(default=default, contracts=contracts)

    def for_symbol(self, symbol: str) -> ContractSpec:
        spec = self.contracts.get(symbol)
        if spec:
            return spec
        return ContractSpec(
            symbol=symbol,
            exchange=self.default.exchange,
            multiplier=self.default.multiplier,
            tick_size=self.default.tick_size,
            margin_rate=self.default.margin_rate,
            commission=self.default.commission,
        )


@dataclass
class FuturesFill:
    realized_pnl: float = 0.0
    opened_quantity: float = 0.0
    closed_today_quantity: float = 0.0
    closed_yesterday_quantity: float = 0.0

    @property
    def closed_quantity(self) -> float:
        return self.closed_today_quantity + self.closed_yesterday_quantity


@dataclass
class FuturesPosition:
    symbol: str
    long_today_quantity: float = 0.0
    long_today_avg_price: float = 0.0
    long_yesterday_quantity: float = 0.0
    long_yesterday_avg_price: float = 0.0
    short_today_quantity: float = 0.0
    short_today_avg_price: float = 0.0
    short_yesterday_quantity: float = 0.0
    short_yesterday_avg_price: float = 0.0
    realized_pnl: float = 0.0
    settlement_pnl: float = 0.0

    @property
    def long_quantity(self) -> float:
        return self.long_today_quantity + self.long_yesterday_quantity

    @property
    def short_quantity(self) -> float:
        return self.short_today_quantity + self.short_yesterday_quantity

    @property
    def long_avg_price(self) -> float:
        return _weighted_average(
            self.long_today_avg_price,
            self.long_today_quantity,
            self.long_yesterday_avg_price,
            self.long_yesterday_quantity,
        )

    @property
    def short_avg_price(self) -> float:
        return _weighted_average(
            self.short_today_avg_price,
            self.short_today_quantity,
            self.short_yesterday_avg_price,
            self.short_yesterday_quantity,
        )

    @property
    def net_quantity(self) -> float:
        return self.long_quantity - self.short_quantity

    def apply_fill(
        self,
        side: Side,
        quantity: float,
        price: float,
        multiplier: float,
        offset: Offset = Offset.AUTO,
    ) -> FuturesFill:
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        fill = FuturesFill()
        remaining = quantity
        closing_only = offset in {Offset.CLOSE, Offset.CLOSE_TODAY, Offset.CLOSE_YESTERDAY}

        if side == Side.BUY and offset != Offset.OPEN:
            remaining = self._close_short(remaining, price, multiplier, offset, fill)

        if side == Side.SELL and offset != Offset.OPEN:
            remaining = self._close_long(remaining, price, multiplier, offset, fill)

        if remaining and not closing_only:
            if side == Side.BUY:
                self.long_today_avg_price = _weighted_average(
                    self.long_today_avg_price,
                    self.long_today_quantity,
                    price,
                    remaining,
                )
                self.long_today_quantity += remaining
            else:
                self.short_today_avg_price = _weighted_average(
                    self.short_today_avg_price,
                    self.short_today_quantity,
                    price,
                    remaining,
                )
                self.short_today_quantity += remaining
            fill.opened_quantity += remaining

        self.realized_pnl += fill.realized_pnl
        return fill

    def unrealized_pnl(self, last_price: float, multiplier: float) -> float:
        long_pnl = (
            (last_price - self.long_today_avg_price) * self.long_today_quantity
            + (last_price - self.long_yesterday_avg_price)
            * self.long_yesterday_quantity
        ) * multiplier
        short_pnl = (
            (self.short_today_avg_price - last_price) * self.short_today_quantity
            + (self.short_yesterday_avg_price - last_price)
            * self.short_yesterday_quantity
        ) * multiplier
        return long_pnl + short_pnl

    def margin(self, last_price: float, spec: ContractSpec) -> float:
        gross_quantity = self.long_quantity + self.short_quantity
        return spec.margin(last_price, gross_quantity)

    def close_available(self, side: Side, offset: Offset) -> float:
        if side == Side.BUY:
            if offset == Offset.CLOSE_TODAY:
                return self.short_today_quantity
            if offset == Offset.CLOSE_YESTERDAY:
                return self.short_yesterday_quantity
            if offset == Offset.CLOSE:
                return self.short_quantity
        if side == Side.SELL:
            if offset == Offset.CLOSE_TODAY:
                return self.long_today_quantity
            if offset == Offset.CLOSE_YESTERDAY:
                return self.long_yesterday_quantity
            if offset == Offset.CLOSE:
                return self.long_quantity
        return float("inf")

    def settle(self, settlement_price: float, multiplier: float) -> float:
        pnl = self.unrealized_pnl(settlement_price, multiplier)
        self.settlement_pnl += pnl

        if self.long_quantity:
            self.long_yesterday_quantity = self.long_quantity
            self.long_yesterday_avg_price = settlement_price
        else:
            self.long_yesterday_quantity = 0.0
            self.long_yesterday_avg_price = 0.0
        self.long_today_quantity = 0.0
        self.long_today_avg_price = 0.0

        if self.short_quantity:
            self.short_yesterday_quantity = self.short_quantity
            self.short_yesterday_avg_price = settlement_price
        else:
            self.short_yesterday_quantity = 0.0
            self.short_yesterday_avg_price = 0.0
        self.short_today_quantity = 0.0
        self.short_today_avg_price = 0.0
        return pnl

    def to_net_position(self) -> Position:
        if self.net_quantity > 0:
            avg_price = self.long_avg_price
        elif self.net_quantity < 0:
            avg_price = self.short_avg_price
        else:
            avg_price = 0.0
        return Position(
            symbol=self.symbol,
            quantity=self.net_quantity,
            avg_price=avg_price,
            realized_pnl=self.realized_pnl + self.settlement_pnl,
        )

    def _close_short(
        self,
        quantity: float,
        price: float,
        multiplier: float,
        offset: Offset,
        fill: FuturesFill,
    ) -> float:
        remaining = quantity
        if offset in {Offset.AUTO, Offset.CLOSE, Offset.CLOSE_TODAY}:
            close_qty = min(remaining, self.short_today_quantity)
            if close_qty:
                fill.realized_pnl += (
                    self.short_today_avg_price - price
                ) * close_qty * multiplier
                fill.closed_today_quantity += close_qty
                self.short_today_quantity -= close_qty
                remaining -= close_qty
                if self.short_today_quantity == 0:
                    self.short_today_avg_price = 0.0
        if remaining and offset in {Offset.AUTO, Offset.CLOSE, Offset.CLOSE_YESTERDAY}:
            close_qty = min(remaining, self.short_yesterday_quantity)
            if close_qty:
                fill.realized_pnl += (
                    self.short_yesterday_avg_price - price
                ) * close_qty * multiplier
                fill.closed_yesterday_quantity += close_qty
                self.short_yesterday_quantity -= close_qty
                remaining -= close_qty
                if self.short_yesterday_quantity == 0:
                    self.short_yesterday_avg_price = 0.0
        return remaining

    def _close_long(
        self,
        quantity: float,
        price: float,
        multiplier: float,
        offset: Offset,
        fill: FuturesFill,
    ) -> float:
        remaining = quantity
        if offset in {Offset.AUTO, Offset.CLOSE, Offset.CLOSE_TODAY}:
            close_qty = min(remaining, self.long_today_quantity)
            if close_qty:
                fill.realized_pnl += (
                    price - self.long_today_avg_price
                ) * close_qty * multiplier
                fill.closed_today_quantity += close_qty
                self.long_today_quantity -= close_qty
                remaining -= close_qty
                if self.long_today_quantity == 0:
                    self.long_today_avg_price = 0.0
        if remaining and offset in {Offset.AUTO, Offset.CLOSE, Offset.CLOSE_YESTERDAY}:
            close_qty = min(remaining, self.long_yesterday_quantity)
            if close_qty:
                fill.realized_pnl += (
                    price - self.long_yesterday_avg_price
                ) * close_qty * multiplier
                fill.closed_yesterday_quantity += close_qty
                self.long_yesterday_quantity -= close_qty
                remaining -= close_qty
                if self.long_yesterday_quantity == 0:
                    self.long_yesterday_avg_price = 0.0
        return remaining


def _weighted_average(old_price: float, old_quantity: float, price: float, quantity: float) -> float:
    new_quantity = old_quantity + quantity
    if new_quantity == 0:
        return 0.0
    return (old_price * old_quantity + price * quantity) / new_quantity


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
