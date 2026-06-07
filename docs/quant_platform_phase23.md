# Quant Platform Phase 23

Phase 23 adds filtered and scheduled CTP reconciliation.

## New Capabilities

- Reconciliation can be limited by instrument symbols.
- Order and trade reconciliation can be limited by CTP time windows.
- CTP query fields now include optional:
  - `InstrumentID`
  - `InsertTimeStart`
  - `InsertTimeEnd`
  - `TradeTimeStart`
  - `TradeTimeEnd`
- `DryRunCtpTransport` applies the same instrument and time filters, which keeps
  local reconciliation tests deterministic.
- `CtpRealtimeEngine.check_watchdog(...)` automatically triggers one lightweight
  reconciliation after watchdog trading recovery succeeds.
- Automatic post-watchdog reconciliation queries positions, orders, and trades,
  but skips account funds by default.

## Configuration

```json
{
  "ctp": {
    "auto_reconcile_after_watchdog_recovery": true
  }
}
```

The automatic reconciliation uses active symbols from subscribed market data,
known ticks, positions, and local orders.

## CLI

Manual filtered reconciliation:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --reconcile `
  --reconcile-symbols RB2405 `
  --reconcile-start-time 09:30:00 `
  --reconcile-end-time 10:00:00
```

Automatic reconciliation after watchdog recovery is controlled by config and is
triggered through the normal watchdog check path:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --watchdog-checks 1
```

## Current Limitations

- Multiple symbol filters use local merge filtering; the CTP query field is
  instrument-specific only when exactly one symbol is supplied.
- Time-window filters apply to orders and trades, not position queries.
- Runtime state persistence across process restarts is covered in Phase 24.
- External scheduling and notification channels are still future work.
