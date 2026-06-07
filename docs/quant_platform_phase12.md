# Quant Platform Phase 12

Phase 12 adds asynchronous CTP query waiting and timeout control.

## New Capabilities

- `CtpEventQueue.wait_for_request(...)` collects callback events by:
  - event type
  - request id
  - `is_last=True`
- `CtpTradingSession` now supports async query completion for:
  - `RSP_QRY_TRADING_ACCOUNT`
  - `RSP_QRY_INVESTOR_POSITION`
- If a query request returns `0` or `None`, the session treats it as accepted
  and waits for matching callbacks.
- Multi-row position responses are accumulated until the callback with
  `is_last=True`.
- CTP callback errors raise `CtpGatewayError` with the CTP error message.
- Query timeout raises `CtpRequestTimeoutError` and records a lifecycle timeout
  event.
- Synchronous dry-run responses still work without waiting.

## Config

```json
{
  "ctp": {
    "query_timeout": 5,
    "wait_for_query_callbacks": true
  }
}
```

## CLI

Override timeout:

```powershell
python -m quant_platform ctp-session `
  --config examples/ctp_futures_config.json `
  --query-timeout 10
```

Send query requests without waiting for callback completion:

```powershell
python -m quant_platform ctp-session `
  --config examples/ctp_futures_config.json `
  --no-wait-query-callbacks
```

## Current Limitations

- Phase 13 extends the same callback-gated pattern to authentication, login,
  and settlement confirmation.
- The wait logic is in-process and depends on the Python CTP binding forwarding
  callbacks into `CtpCallbackAdapter`.
- Market data query/subscription callbacks are still future work.
