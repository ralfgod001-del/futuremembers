# Quant Platform Phase 1

This phase implements a small but complete research loop:

- CSV OHLCV data import.
- Event-driven strategy callbacks.
- Next-bar-open market order fills.
- Cash, net position, commission, slippage, equity curve.
- Backtest metrics and CSV/JSON report export.
- A moving-average crossover sample strategy.

## Commands

Generate deterministic sample data:

```powershell
python -m quant_platform generate-sample --output data/sample_bars.csv
```

Run the sample strategy:

```powershell
python -m quant_platform backtest --data data/sample_bars.csv --output output/backtests/ma_cross
```

Customize parameters:

```powershell
python -m quant_platform backtest --data data/sample_bars.csv --params "{\"fast_window\": 10, \"slow_window\": 30, \"quantity\": 2}"
```

## Strategy Contract

Strategies subclass `quant_platform.strategy.Strategy` and can implement:

- `on_init(context)`
- `on_bar(context, bar)`
- `on_order(context, order)`
- `on_trade(context, trade)`
- `on_finish(context)`

Orders submitted inside `on_bar` are filled at the next available bar open for that symbol.
