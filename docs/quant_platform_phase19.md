# Quant Platform Phase 19

Phase 19 adds CTP front connection health tracking.

## New Capabilities

- Trading callback adapter supports:
  - `OnFrontConnected`
  - `OnFrontDisconnected`
- Market data callback adapter supports:
  - `OnFrontConnected`
  - `OnFrontDisconnected`
- `CtpTradingSession` tracks:
  - `front_connected`
  - `last_disconnect_reason`
  - disconnected state
- `CtpMarketDataSession` tracks the same front health fields.
- On trading-front disconnect, the session clears authenticated, logged-in, and
  settlement-confirmed flags.
- On market-data-front disconnect, the session clears logged-in state but keeps
  `subscribed_symbols` so future reconnect logic can resubscribe.
- Session snapshots now expose connection health fields.

## CLI

Simulate front disconnects in a realtime dry run:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-disconnect
```

## Current Limitations

- Automatic recovery is covered in Phase 20.
