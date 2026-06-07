# Quant Platform Phase 2

Phase 2 adds the research features needed before a UI or live gateway:

- Multi-symbol sample data generation.
- JSON-configured backtests.
- Per-symbol commission, slippage, and minimum commission settings.
- Grid-search parameter optimization.
- HTML backtest reports with equity, drawdown, metrics, and recent trades.

## Generate Multi-Symbol Data

```powershell
python -m quant_platform generate-sample --symbols SAMPLE_A SAMPLE_B SAMPLE_C --output data/sample_multi_bars.csv --periods 260
```

## Run A Configured Backtest

```powershell
python -m quant_platform backtest --config examples/ma_cross_config.json
```

Outputs:

- `summary.json`
- `equity_curve.csv`
- `orders.csv`
- `trades.csv`
- `positions.csv`
- `event_log.csv`
- `report.html`

## Run Parameter Optimization

```powershell
python -m quant_platform optimize --config examples/ma_cross_optimization.json
```

The optimizer maximizes the configured metric and exports:

- `optimization_results.csv`
- `best_params.json`
- `best_backtest/report.html`

## Config Shape

```json
{
  "data": [{"path": "data/sample_multi_bars.csv"}],
  "strategy": "sample:ma_cross",
  "params": {"fast_window": 8, "slow_window": 24, "quantity": 1},
  "engine": {"cash": 100000, "commission_rate": 0.0002, "slippage": 0},
  "execution": {
    "default": {"commission_rate": 0.0002, "slippage": 0.01},
    "symbols": {
      "SAMPLE_B": {"commission_rate": 0.0003, "slippage": 0.02}
    }
  },
  "optimization": {
    "objective": "sharpe",
    "grid": {"fast_window": [5, 8], "slow_window": [20, 24]}
  },
  "output": "output/backtests/example"
}
```
