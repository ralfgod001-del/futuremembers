# Quant Platform Phase 16

Phase 16 closes the realtime CTP event loop by dispatching order and trade
returns back into strategies.

## New Capabilities

- `CtpTradingSession` can register order and trade return handlers.
- `CtpCallbackAdapter.OnRtnOrder` now notifies those handlers after normalizing
  the CTP order return.
- `CtpCallbackAdapter.OnRtnTrade` now notifies those handlers after normalizing
  the CTP trade return.
- `CtpFuturesGateway.local_order_id_for_order_ref(...)` maps CTP `OrderRef`
  values back to local strategy order ids.
- `CtpRealtimeEngine` now:
  - tracks local orders by id
  - maps CTP order refs to local order ids
  - updates local order status from CTP order returns
  - builds local `Trade` objects from CTP trade returns
  - dispatches `strategy.on_order(...)` and `strategy.on_trade(...)`
- Realtime snapshots include normalized trades.

## CLI

Run a dry-run realtime check with simulated tick, order return, and trade return:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-fill
```

## Current Limitations

- Phase 17 adds `OnRspOrderInsert` and `OnRspOrderAction` error dispatch plus
  realtime cancel submission/return handling.
- Partial fill aggregation is basic: order status becomes filled when received
  trade quantity reaches the local order quantity.
- Realtime position/account refresh still depends on CTP position/account
  callbacks or explicit queries.
