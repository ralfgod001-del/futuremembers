# Quant Platform Phase 15

Phase 15 connects CTP ticks to realtime strategy dispatch.

## New Capabilities

- `Strategy` now has an optional `on_tick(context, tick)` hook.
- `StrategyContext` exposes `last_tick(symbol)` alongside `last_price(symbol)`.
- `CtpMarketDataSession` can register tick handlers and dispatch normalized
  `Tick` objects as CTP market data callbacks arrive.
- `CtpRealtimeEngine` wires together:
  - `CtpTradingSession`
  - `CtpMarketDataSession`
  - `Strategy`
  - `RiskManager`
- Realtime strategy orders go through the same CTP order insert path as manual
  CTP orders.
- Risk checks run before `ReqOrderInsert`; rejected realtime orders are recorded
  locally and never sent to the CTP transport.
- Added `BuyFirstTickStrategy` for dry-run smoke checks.

## CLI

Run a dry-run realtime dispatch check with one synthetic tick:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --simulate-tick
```

Use a custom tick-aware strategy:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --strategy your_module:YourStrategy `
  --params "{\"quantity\":1}" `
  --symbols RB2405
```

## Current Limitations

- Phase 16 dispatches CTP order and trade returns back into realtime strategies.
- The realtime engine currently keeps tick history as latest tick only; bar
  aggregation from live ticks is still future work.
- Reconnect, heartbeat, and automatic resubscribe are still future work.
