# Quant Platform Phase 11

Phase 11 adds the CTP callback event queue for the futures-only counter path.

## New Capabilities

- `CtpCallbackAdapter` exposes CTP-style callback methods:
  - `OnRspAuthenticate`
  - `OnRspUserLogin`
  - `OnRspSettlementInfoConfirm`
  - `OnRspQryTradingAccount`
  - `OnRspQryInvestorPosition`
  - `OnRtnOrder`
  - `OnRtnTrade`
  - `OnRspOrderInsert`
  - `OnRspOrderAction`
  - `OnRspError`
- `CtpEventQueue` stores normalized callback events.
- Callback events include:
  - event type
  - request id
  - success flag
  - last-row flag
  - sanitized payload
  - sanitized CTP response info
- Account callbacks update `CtpTradingAccount`.
- Position callbacks aggregate rows into `FuturesPosition` today/yesterday buckets.
- Order and trade returns update gateway order/trade maps.
- `NativeCtpTraderTransport` attempts to register the callback adapter through
  `RegisterSpi`, `register_spi`, `set_callback`, or `SetCallback`.

## Callback Demo

Run a dry-run session and emit synthetic callbacks:

```powershell
python -m quant_platform ctp-session `
  --config examples/ctp_futures_config.json `
  --simulate-callbacks
```

The output includes `callback_events`, account data, position buckets, order
returns, and trade returns.

## Current Limitations

- The real CTP callback thread is still owned by the external Python binding.
- Phase 12 adds request-id based query waiting and timeout control.
- Market data SPI callbacks are still future work.
- Settlement info query and manual review before confirm are still pending.
