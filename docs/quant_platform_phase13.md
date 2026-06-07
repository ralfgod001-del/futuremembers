# Quant Platform Phase 13

Phase 13 makes CTP startup callback-gated instead of request-submission gated.

## New Capabilities

- `CtpTradingSession.start(...)` now waits for successful callbacks before
  marking the lifecycle state as ready:
  - `RSP_AUTHENTICATE`
  - `RSP_USER_LOGIN`
  - `RSP_SETTLEMENT_INFO_CONFIRM`
- Lifecycle callback errors raise `CtpGatewayError` with the CTP error message.
- Lifecycle timeout raises `CtpRequestTimeoutError` and records a timeout event.
- `DryRunCtpTransport` emits synchronous lifecycle callbacks so local checks
  still complete without a real CTP front.
- Query callback waiting from Phase 12 remains unchanged.

## Config

```json
{
  "ctp": {
    "lifecycle_timeout": 5,
    "wait_for_lifecycle_callbacks": true,
    "query_timeout": 5,
    "wait_for_query_callbacks": true
  }
}
```

## CLI

Override lifecycle timeout:

```powershell
python -m quant_platform ctp-session `
  --config examples/ctp_futures_config.json `
  --lifecycle-timeout 10
```

Send lifecycle requests without waiting for callback completion:

```powershell
python -m quant_platform ctp-session `
  --config examples/ctp_futures_config.json `
  --no-wait-lifecycle-callbacks
```

## Current Limitations

- The callback adapter must be registered by the Python CTP binding; unsupported
  wrapper APIs still need a small transport adapter.
- Phase 14 adds a separate CTP market data session for login, subscription, and
  depth tick normalization.
