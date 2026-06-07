# Quant Platform Phase 29

Phase 29 adds a local CTP realtime monitor to the existing web workspace.

## New Capabilities

- `quant_platform serve` now exposes `GET /api/ctp-monitor`.
- The monitor reads:
  - `output/ctp_realtime_state.json` by default
  - `output/ctp_events.jsonl` by default
- The API summarizes:
  - order counts and status counts
  - working orders
  - trades and notional
  - latest tick symbols and timestamps
  - strategy state metadata
  - watchdog trading and market data health
  - latest JSONL event rows
  - rotated event log backup metadata
- The web UI now includes a CTP Monitor panel with editable state/event paths.

## Usage

Generate a dry-run realtime state and event log:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --simulate-fill `
  --save-state `
  --event-log-path output/ctp_events.jsonl
```

Start the workspace:

```powershell
python -m quant_platform serve --workspace .
```

Open:

```text
http://127.0.0.1:8765
```

The CTP Monitor panel will read the default files. Use the sidebar inputs when
the realtime runner writes to a custom state or event path.

## API Example

```text
GET /api/ctp-monitor?state_path=output/ctp_realtime_state.json&event_log_path=output/ctp_events.jsonl&limit=80
```

## Current Limitations

- The monitor is file-based; it does not yet stream directly from a running
  CTP process.
- Refresh is manual from the web UI.
- Only the active JSONL event file is tailed; rotated backups are listed but not
  merged into the event table.
