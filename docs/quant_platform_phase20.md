# Quant Platform Phase 20

Phase 20 adds automatic CTP recovery after a trading or market-data front reconnect.

## New Capabilities

- `CtpTradingSession` remembers the last start options and can recover after
  `OnFrontConnected`.
- Trading recovery replays the lifecycle steps that were enabled at startup:
  - authentication
  - user login
  - settlement confirmation
  - account query
  - position query
- `CtpMarketDataSession` keeps the subscribed symbol set through a disconnect.
- Market data recovery re-logins and automatically resubscribes preserved
  instruments.
- Session snapshots continue exposing `front_connected` and
  `last_disconnect_reason`, while lifecycle events include:
  - `AUTO_RECOVER_START`
  - `AUTO_RECOVER_READY`
  - `AUTO_RECOVER_ERROR`
  - `AUTO_MD_RECOVER_START`
  - `AUTO_MD_RECOVER_READY`
  - `AUTO_MD_RECOVER_ERROR`

## Configuration

```json
{
  "ctp": {
    "auto_recover_on_front_connected": true,
    "auto_resubscribe_on_front_connected": true
  }
}
```

Set `auto_recover_on_front_connected` to `false` to keep Phase 19 style health
tracking without automatic re-login. Set `auto_resubscribe_on_front_connected`
to `false` if market data reconnect should only login and leave subscription
management to an outer supervisor.

## CLI

Simulate disconnect-only health state:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-disconnect
```

Simulate disconnect plus reconnect recovery:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-reconnect
```

## Current Limitations

- Long-running watchdog checks and reconnect backoff are covered in Phase 21.
- In-flight orders are not actively reconciled after reconnect beyond the normal
  CTP order/trade callbacks already supported.
- Market data resubscription uses the local subscribed-symbol set; it does not
  yet diff against exchange-side subscription state.
