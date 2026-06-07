from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SymbolExecutionConfig:
    commission_rate: float = 0.0002
    slippage: float = 0.0
    min_commission: float = 0.0

    @classmethod
    def from_mapping(
        cls,
        raw: dict[str, Any] | None,
        base: "SymbolExecutionConfig | None" = None,
    ) -> "SymbolExecutionConfig":
        source = raw or {}
        fallback = base or cls()
        return cls(
            commission_rate=float(source.get("commission_rate", fallback.commission_rate)),
            slippage=float(source.get("slippage", fallback.slippage)),
            min_commission=float(source.get("min_commission", fallback.min_commission)),
        )


@dataclass
class ExecutionConfig:
    default: SymbolExecutionConfig = field(default_factory=SymbolExecutionConfig)
    symbols: dict[str, SymbolExecutionConfig] = field(default_factory=dict)

    @classmethod
    def from_legacy(
        cls,
        commission_rate: float = 0.0002,
        slippage: float = 0.0,
    ) -> "ExecutionConfig":
        return cls(
            default=SymbolExecutionConfig(
                commission_rate=float(commission_rate),
                slippage=float(slippage),
            )
        )

    @classmethod
    def from_mapping(
        cls,
        raw: dict[str, Any] | None,
        fallback_commission_rate: float = 0.0002,
        fallback_slippage: float = 0.0,
    ) -> "ExecutionConfig":
        if not raw:
            return cls.from_legacy(fallback_commission_rate, fallback_slippage)

        base = SymbolExecutionConfig(
            commission_rate=float(raw.get("commission_rate", fallback_commission_rate)),
            slippage=float(raw.get("slippage", fallback_slippage)),
            min_commission=float(raw.get("min_commission", 0.0)),
        )
        default = SymbolExecutionConfig.from_mapping(raw.get("default"), base)
        symbols = {
            symbol: SymbolExecutionConfig.from_mapping(symbol_raw, default)
            for symbol, symbol_raw in raw.get("symbols", {}).items()
        }
        return cls(default=default, symbols=symbols)

    def for_symbol(self, symbol: str) -> SymbolExecutionConfig:
        return self.symbols.get(symbol, self.default)
