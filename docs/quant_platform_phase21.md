# Quant Platform Phase 21

Phase 21 adds a CTP realtime watchdog for long-running futures sessions.

## New Capabilities

- `CtpSessionWatchdog` monitors the trading and market-data sessions together.
- The watchdog detects unhealthy but front-connected sessions and triggers the
  Phase 20 recovery path.
- Failed recovery attempts are retried with configurable exponential backoff.
- Recovery attempt limits emit explicit give-up events instead of retrying
  forever.
- `CtpRealtimeEngine` now owns a watchdog and exposes its snapshot under
  `watchdog`.
- Realtime engine events mirror watchdog checks, so future UI/logging layers can
  consume health transitions without reading CTP internals.

## Watchdog Events

- `WATCHDOG_TRADING_HEALTHY`
- `WATCHDOG_TRADING_WAITING_FOR_FRONT`
- `WATCHDOG_TRADING_RECOVER_START`
- `WATCHDOG_TRADING_RECOVER_READY`
- `WATCHDOG_TRADING_RETRY_SCHEDULED`
- `WATCHDOG_TRADING_BACKOFF`
- `WATCHDOG_TRADING_GIVE_UP`
- `WATCHDOG_MARKET_DATA_HEALTHY`
- `WATCHDOG_MARKET_DATA_WAITING_FOR_FRONT`
- `WATCHDOG_MARKET_DATA_RECOVER_START`
- `WATCHDOG_MARKET_DATA_RECOVER_READY`
- `WATCHDOG_MARKET_DATA_RETRY_SCHEDULED`
- `WATCHDOG_MARKET_DATA_BACKOFF`
- `WATCHDOG_MARKET_DATA_GIVE_UP`

## Configuration

```json
{
  "ctp": {
    "watchdog_check_interval": 5,
    "watchdog_initial_backoff": 1,
    "watchdog_max_backoff": 30,
    "watchdog_backoff_multiplier": 2,
    "watchdog_max_recovery_attempts": 3
  }
}
```

## CLI

Run one watchdog check after a dry-run reconnect:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-reconnect `
  --watchdog-checks 1
```

For real long-running use, call `engine.check_watchdog(force=False)` from the
outer event loop or scheduler. The watchdog respects `watchdog_check_interval`
when `force=False`.

## Current Limitations

- The watchdog retries logical recovery after CTP front callbacks; it does not
  replace the native CTP API's own TCP reconnect mechanism.
- Order, trade, and position reconciliation after recovery is covered in Phase 22.
- Alerts are recorded as events only; external notification channels are still
  future work.
