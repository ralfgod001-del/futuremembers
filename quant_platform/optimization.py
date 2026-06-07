from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from .backtest import BacktestEngine, BacktestResult
from .execution import ExecutionConfig
from .futures import ContractRegistry
from .models import Bar
from .risk import RiskManager
from .strategy import Strategy


StrategyFactory = Callable[[dict[str, Any]], Strategy]


@dataclass
class GridSearchResult:
    results: pd.DataFrame
    best_result: BacktestResult | None
    best_params: dict[str, Any]
    objective: str

    def export(self, output_dir: str | Path) -> dict[str, Path]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        results_path = out_dir / "optimization_results.csv"
        best_path = out_dir / "best_params.json"
        self.results.to_csv(results_path, index=False)
        best_payload = {
            "objective": self.objective,
            "best_params": self.best_params,
        }
        if self.best_result is not None:
            best_payload["metrics"] = self.best_result.metrics
            self.best_result.export(out_dir / "best_backtest")
        best_path.write_text(json.dumps(best_payload, indent=2), encoding="utf-8")
        return {
            "results": results_path,
            "best_params": best_path,
        }


def expand_grid(param_grid: dict[str, Iterable[Any]]) -> list[dict[str, Any]]:
    if not param_grid:
        return [{}]

    keys = list(param_grid)
    values = [list(param_grid[key]) for key in keys]
    if any(not items for items in values):
        raise ValueError("optimization grid values cannot be empty")
    return [dict(zip(keys, combo)) for combo in product(*values)]


def run_grid_search(
    bars: list[Bar],
    strategy_factory: StrategyFactory,
    param_grid: dict[str, Iterable[Any]],
    base_params: dict[str, Any] | None = None,
    initial_cash: float = 100_000.0,
    execution_config: ExecutionConfig | None = None,
    risk_manager: RiskManager | None = None,
    account_mode: str = "cash",
    contract_registry: ContractRegistry | None = None,
    daily_settlement: bool = False,
    commission_rate: float = 0.0002,
    slippage: float = 0.0,
    objective: str = "sharpe",
) -> GridSearchResult:
    rows: list[dict[str, Any]] = []
    best_result: BacktestResult | None = None
    best_params: dict[str, Any] = {}
    best_score: float | None = None
    base = base_params or {}

    for params in expand_grid(param_grid):
        merged_params = {**base, **params}
        strategy = strategy_factory(merged_params)
        engine = BacktestEngine(
            bars=bars,
            strategy=strategy,
            initial_cash=initial_cash,
            commission_rate=commission_rate,
            slippage=slippage,
            execution_config=execution_config,
            risk_manager=risk_manager,
            account_mode=account_mode,
            contract_registry=contract_registry,
            daily_settlement=daily_settlement,
        )
        result = engine.run()
        if objective not in result.metrics:
            raise ValueError(f"unknown optimization objective: {objective}")

        score = float(result.metrics[objective])
        row = {
            "params": json.dumps(merged_params, sort_keys=True),
            **merged_params,
            **result.metrics,
        }
        rows.append(row)

        if best_score is None or score > best_score:
            best_score = score
            best_result = result
            best_params = merged_params

    results = pd.DataFrame(rows)
    if not results.empty:
        results = results.sort_values(objective, ascending=False).reset_index(drop=True)
    return GridSearchResult(
        results=results,
        best_result=best_result,
        best_params=best_params,
        objective=objective,
    )
