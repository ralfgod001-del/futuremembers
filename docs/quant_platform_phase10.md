# Quant Platform Phase 10

Phase 10 adds the futures-only CTP runtime session layer.

## New Capabilities

- `CtpTradingSession` orchestrates the CTP lifecycle:
  - connect
  - authenticate
  - login
  - settlement confirmation
  - query trading account
  - query investor positions
- `DryRunCtpTransport` runs the same lifecycle without loading a CTP library or
  sending orders.
- `NativeCtpTraderTransport` is a thin optional adapter for a Python CTP binding.
- Session events redact `Password` and `AuthCode`.
- The existing `CtpFuturesGateway` is reused for order insert and cancel requests.
- No securities broker path is included.

## Dry-Run Session

```powershell
python -m quant_platform ctp-session --config examples/ctp_futures_config.json
```

This prints a lifecycle snapshot with:

- connection state
- login/settlement-confirm flags
- account snapshot
- position snapshot
- lifecycle events

## Live Session Skeleton

`--live` asks the platform to import the configured Python CTP module:

```powershell
python -m quant_platform ctp-session `
  --config examples/ctp_futures_config.json `
  --live `
  --transport-module your_ctp_python_module `
  --trader-api-factory CreateFtdcTraderApi
```

The native transport looks for common CTP method names such as:

- `RegisterFront`
- `Init`
- `ReqAuthenticate`
- `ReqUserLogin`
- `ReqSettlementInfoConfirm`
- `ReqQryTradingAccount`
- `ReqQryInvestorPosition`
- `ReqOrderInsert`
- `ReqOrderAction`

If the module is missing, live mode fails before attempting any trading action.

## Current Limitations

- Phase 11 adds the callback adapter and normalized event queue.
- Query responses are asynchronous in real CTP; the dry-run transport returns
  immediate synthetic data for testability.
- Settlement info query/print/confirm review workflow is not implemented yet.
- Market data subscription is still separate future work.
