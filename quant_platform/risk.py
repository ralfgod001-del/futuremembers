from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Side


@dataclass(frozen=True)
class SymbolRiskConfig:
    max_order_quantity: float | None = None
    max_position_quantity: float | None = None
    max_order_notional: float | None = None
    allow_short: bool = True

    @classmethod
    def from_mapping(
        cls,
        raw: dict[str, Any] | None,
        base: "SymbolRiskConfig | None" = None,
    ) -> "SymbolRiskConfig":
        source = raw or {}
        fallback = base or cls()
        return cls(
            max_order_quantity=_optional_float(
                source.get("max_order_quantity", fallback.max_order_quantity)
            ),
            max_position_quantity=_optional_float(
                source.get("max_position_quantity", fallback.max_position_quantity)
            ),
            max_order_notional=_optional_float(
                source.get("max_order_notional", fallback.max_order_notional)
            ),
            allow_short=bool(source.get("allow_short", fallback.allow_short)),
        )


@dataclass
class RiskConfig:
    enabled: bool = True
    max_drawdown: float | None = None
    default: SymbolRiskConfig = field(default_factory=SymbolRiskConfig)
    symbols: dict[str, SymbolRiskConfig] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "RiskConfig":
        if not raw:
            return cls()

        base = SymbolRiskConfig(
            max_order_quantity=_optional_float(raw.get("max_order_quantity")),
            max_position_quantity=_optional_float(raw.get("max_position_quantity")),
            max_order_notional=_optional_float(raw.get("max_order_notional")),
            allow_short=bool(raw.get("allow_short", True)),
        )
        default = SymbolRiskConfig.from_mapping(raw.get("default"), base)
        symbols = {
            symbol: SymbolRiskConfig.from_mapping(symbol_raw, default)
            for symbol, symbol_raw in raw.get("symbols", {}).items()
        }
        return cls(
            enabled=bool(raw.get("enabled", True)),
            max_drawdown=_optional_float(raw.get("max_drawdown")),
            default=default,
            symbols=symbols,
        )

    def for_symbol(self, symbol: str) -> SymbolRiskConfig:
        return self.symbols.get(symbol, self.default)


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "RiskManager":
        return cls(RiskConfig.from_mapping(raw))

    def check_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        current_position: float,
        reference_price: float | None,
        current_equity: float,
        initial_cash: float,
    ) -> str | None:
        if not self.config.enabled:
            return None

        if self.config.max_drawdown is not None:
            floor = initial_cash * (1 - self.config.max_drawdown)
            if current_equity < floor:
                return f"max drawdown breached: equity {current_equity:.2f} < {floor:.2f}"

        rule = self.config.for_symbol(symbol)
        if rule.max_order_quantity is not None and quantity > rule.max_order_quantity:
            return (
                f"order quantity {quantity:g} exceeds max "
                f"{rule.max_order_quantity:g}"
            )

        signed_quantity = quantity if side == Side.BUY else -quantity
        projected_position = current_position + signed_quantity
        if not rule.allow_short and projected_position < 0:
            return "short position is not allowed"

        if (
            rule.max_position_quantity is not None
            and abs(projected_position) > rule.max_position_quantity
        ):
            return (
                f"projected position {projected_position:g} exceeds max "
                f"{rule.max_position_quantity:g}"
            )

        if (
            rule.max_order_notional is not None
            and reference_price is not None
            and abs(reference_price * quantity) > rule.max_order_notional
        ):
            return (
                f"order notional {abs(reference_price * quantity):.2f} exceeds max "
                f"{rule.max_order_notional:.2f}"
            )

        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
