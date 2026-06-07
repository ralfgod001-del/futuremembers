# Quant Platform Phase 17

Phase 17 adds realtime order error handling and cancel flow closure for CTP.

## New Capabilities

- `CtpCallbackAdapter.OnRspOrderInsert` now dispatches rejected insert responses
  to `CtpTradingSession`.
- `CtpCallbackAdapter.OnRspOrderAction` now dispatches rejected cancel responses
  to `CtpTradingSession`.
- `CtpTradingSession` exposes handlers for:
  - order returns
  - trade returns
  - order insert errors
  - order action errors
- `CtpFuturesGateway` can map CTP request ids and order refs back to local order
  ids.
- `CtpRealtimeEngine.cancel_order(...)` submits CTP cancel requests and records
  cancel lifecycle events.
- Realtime orders are updated for:
  - order insert rejection
  - cancel submission
  - cancel rejection
  - canceled order return

## CLI

Simulate an insert rejection:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-insert-error
```

Simulate a successful cancel:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-cancel
```

Simulate a cancel rejection:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-cancel-error
```

## Current Limitations

- Partial fills still use a simple cumulative quantity check.
- Cancel requests use the local order mapping and basic CTP action fields; richer
  broker-specific cancel identifiers can be added when testing against a real
 柜台 wrapper.
- Phase 18 adds live tick-to-minute-bar aggregation for realtime strategies.
- Reconnect and automatic resubscribe/requery are still future work.
