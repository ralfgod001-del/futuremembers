from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .backtest import BacktestEngine, BacktestResult
from .execution import ExecutionConfig
from .futures import ContractRegistry
from .models import Bar
from .risk import RiskManager
from .strategy import Strategy


@dataclass
class ReplayResult:
    result: BacktestResult
    steps: int

    def export(self, output_dir: str | Path) -> dict[str, Path]:
        return self.result.export(output_dir)


def run_market_replay(
    bars: list[Bar],
    strategy: Strategy,
    initial_cash: float = 100_000.0,
    execution_config: ExecutionConfig | None = None,
    risk_manager: RiskManager | None = None,
    account_mode: str = "cash",
    contract_registry: ContractRegistry | None = None,
    daily_settlement: bool = False,
    max_steps: int | None = None,
) -> ReplayResult:
    replay_bars = _limit_by_steps(bars, max_steps)
    engine = BacktestEngine(
        bars=replay_bars,
        strategy=strategy,
        initial_cash=initial_cash,
        execution_config=execution_config,
        risk_manager=risk_manager,
        account_mode=account_mode,
        contract_registry=contract_registry,
        daily_settlement=daily_settlement,
        record_bars=True,
    )
    return ReplayResult(
        result=engine.run(),
        steps=len({bar.timestamp for bar in replay_bars}),
    )


def _limit_by_steps(bars: list[Bar], max_steps: int | None) -> list[Bar]:
    sorted_bars = sorted(bars, key=lambda item: (item.timestamp, item.symbol))
    if max_steps is None:
        return sorted_bars

    allowed_timestamps = set()
    for bar in sorted_bars:
        allowed_timestamps.add(bar.timestamp)
        if len(allowed_timestamps) >= max_steps:
            break
    return [bar for bar in sorted_bars if bar.timestamp in allowed_timestamps]
