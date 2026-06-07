# Quant Platform Phase 30

Phase 30 adds automatic refresh and health grading to the CTP realtime monitor.

## New Capabilities

- `/api/ctp-monitor` now accepts `stale_seconds`.
- The monitor summary now includes:
  - `healthStatus`: `OK`, `WARN`, or `ERROR`
  - `alerts`: normalized monitor alerts
  - `stateAgeSeconds`
  - `staleSeconds`
- The backend raises alerts for:
  - missing runtime state
  - stale runtime state
  - missing or empty event logs
  - unhealthy trading or market data connection snapshots
  - missing market data subscriptions
  - rejected orders
  - error, reject, timeout, disconnect, backoff, and give-up events
- The Web UI now has:
  - a health status pill
  - an alert list
  - an automatic refresh checkbox
  - refresh interval and stale threshold inputs

## Usage

Start the workspace:

```powershell
python -m quant_platform serve --workspace .
```

The CTP Monitor panel refreshes automatically every 5 seconds by default. To
adjust how quickly a saved state is considered stale, change `Stale Seconds` in
the sidebar or call the API directly:

```text
GET /api/ctp-monitor?stale_seconds=300
```

## Current Limitations

- Automatic refresh is browser-side polling, not server-push streaming.
- Alerts are shown in the local Web UI and API only; external notification
  channels are not implemented yet.
- Health grading depends on the latest saved state and event log, so a running
  process should save state regularly for accurate monitoring.
