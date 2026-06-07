# Quant Platform Phase 27

Phase 27 adds persistent realtime event logs.

## New Capabilities

- `EventRecorder.enable_jsonl(path)` appends each new runtime event to a JSONL
  file as it is recorded.
- `EventRecorder.export_csv(path)` exports the in-memory event table to CSV.
- `CtpRealtimeEngine.enable_event_log(path)` enables append-only JSONL logging
  for realtime events.
- `CtpRealtimeEngine.export_event_log_csv(path)` writes a CSV snapshot of the
  current in-memory event log.
- JSONL events include:
  - timestamp
  - event type
  - severity
  - symbol
  - order id
  - trade id
  - message
  - payload
- The JSONL path captures later events such as:
  - `RUN_START`
  - `ORDER_SUBMITTED`
  - `ORDER_REJECTED`
  - `WATCHDOG_*`
  - `RECONCILE_*`
  - `STATE_LOADED`
  - `STATE_SAVED`
  - `STRATEGY_STATE_*`

## CLI

Append realtime events to JSONL:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --event-log-path output/ctp_events.jsonl
```

Also export a CSV snapshot before printing the final JSON snapshot:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --event-log-path output/ctp_events.jsonl `
  --event-log-csv output/ctp_events.csv
```

## Current Limitations

- JSONL rotation and backup retention are covered in Phase 28.
- CSV export is a snapshot of in-memory events at export time.
- There is no external log shipping or retention policy yet.
