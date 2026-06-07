# Quant Platform Phase 8

Phase 8 adds a paper trading layer. It is a simulated broker gateway that lets
the same strategy interface run closer to an intraday operator workflow than a
pure historical backtest.

## New Capabilities

- `PaperBrokerGateway` accepts, rejects, cancels, and fills orders.
- `PaperTradingEngine` runs strategies through that gateway.
- Market orders fill against the latest bar close.
- Limit orders remain working until the latest price crosses the limit.
- Strategies can call `context.engine.cancel_order(order_id)` during paper runs.
- Cash and futures account modes share the existing position, commission, risk,
  margin, and daily settlement rules.
- Paper sessions export the same CSV, JSON, and HTML report files as backtests.

## Command Line

```powershell
python -m quant_platform paper --config examples/paper_config.json --output output/paper/phase8_demo
```

Limit the session by timestamp count:

```powershell
python -m quant_platform paper --config examples/paper_config.json --max-steps 80 --stream
```

## Web Workspace

The local workspace now supports four modes:

- Backtest
- Replay
- Paper
- Optimize

Start it with:

```powershell
python -m quant_platform serve --host 127.0.0.1 --port 8765
```

Then choose `paper_config` or switch any compatible config to `Paper` mode.

## Backtest vs Paper Semantics

- Backtest orders submitted in `on_bar` fill at the next available bar open.
- Paper market orders submitted in `on_bar` fill at the current latest close.
- Paper limit orders can stay pending across bars and can be canceled by
  strategy logic.

This separation keeps research backtests deterministic while giving the
operator console a more broker-like order lifecycle.

## Current Limitations

- The gateway is in-process and simulated; no real broker connection is made.
- Partial fills and queue priority are not modeled yet.
- Tick-level paper trading can be added once tick data ingestion is available.
- CTP-specific order, trade, account, and position field mapping starts in Phase 9.
