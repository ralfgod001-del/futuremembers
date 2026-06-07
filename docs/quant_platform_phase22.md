# Quant Platform Phase 22

Phase 22 adds CTP reconciliation after reconnect or manual health checks.

## New Capabilities

- `CtpTransportProtocol` now includes:
  - `query_orders`
  - `query_trades`
- `NativeCtpTraderTransport` maps these to:
  - `ReqQryOrder`
  - `ReqQryTrade`
- `DryRunCtpTransport` supports `order_responses` and `trade_responses` for
  deterministic reconciliation tests.
- `CtpCallbackAdapter` supports:
  - `OnRspQryOrder`
  - `OnRspQryTrade`
- `CtpTradingSession.reconcile(...)` can query account, positions, orders, and
  trades in one explicit reconciliation step.
- `CtpRealtimeEngine.reconcile(...)` merges queried CTP orders and trades back
  into local realtime orders and trades.

## Realtime Events

- `RECONCILE_START`
- `ORDER_RECONCILED`
- `ORDER_RECONCILE_UNMATCHED`
- `TRADE_RECONCILED`
- `TRADE_RECONCILE_UNMATCHED`
- `RECONCILE_READY`
- `RECONCILE_ERROR`

## CLI

Run reconciliation after a dry-run reconnect:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-reconnect `
  --watchdog-checks 1 `
  --reconcile
```

In live mode, `--reconcile` issues CTP account, position, order, and trade
queries through the configured trader API binding.

## Current Limitations

- Symbol and time-window reconciliation filters are covered in Phase 23.
- Reconciliation maps queried orders and trades to local orders through CTP
  `OrderRef`; broker-side historical orders that do not belong to this process
  are reported as unmatched.
- Duplicate trade IDs are ignored when merging into local realtime state.
