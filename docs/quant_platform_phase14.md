# Quant Platform Phase 14

Phase 14 adds the futures CTP market data side.

## New Capabilities

- Added a normalized `Tick` model for live quote updates.
- `CtpMarketDataSession` handles market data front lifecycle:
  - connect to `md_front`
  - login through the CTP MD API
  - subscribe and unsubscribe instruments
- `CtpMarketDataCallbackAdapter` supports:
  - `OnRspUserLogin`
  - `OnRspSubMarketData`
  - `OnRspUnSubMarketData`
  - `OnRtnDepthMarketData`
- Depth market data is normalized by `ctp_depth_market_data_to_tick(...)` and
  stored in `CtpFuturesGateway.ticks`.
- `DryRunCtpMarketDataTransport` and `NativeCtpMarketDataTransport` mirror the
  trading-side dry-run/native split.

## Config

```json
{
  "ctp": {
    "md_front": "tcp://YOUR_MD_FRONT:41213",
    "md_transport_module": "",
    "md_api_factory": "CreateFtdcMdApi",
    "market_data_timeout": 5,
    "wait_for_market_data_callbacks": true
  }
}
```

## CLI

Subscribe the configured contracts in dry-run mode and emit one synthetic tick:

```powershell
python -m quant_platform ctp-md `
  --config examples/ctp_futures_config.json `
  --simulate-tick
```

Subscribe selected instruments:

```powershell
python -m quant_platform ctp-md `
  --config examples/ctp_futures_config.json `
  --symbols RB2405
```

## Current Limitations

- Subscription acknowledgement is recorded but not yet callback-gated because
  common CTP MD wrappers do not pass a request id through `SubscribeMarketData`.
- Phase 15 adds Tick-to-strategy dispatch through `CtpRealtimeEngine`.
- Market data reconnect and resubscribe recovery are still future work.
