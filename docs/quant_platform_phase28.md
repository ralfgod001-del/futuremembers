# Quant Platform Phase 28

Phase 28 adds JSONL event log rotation and backup retention.

## New Capabilities

- `EventRecorder.enable_jsonl(...)` now accepts:
  - `max_bytes`
  - `backup_count`
- Before writing a new JSONL event, the recorder checks whether the active log
  would exceed `max_bytes`.
- If rotation is needed:
  - `events.jsonl` becomes `events.jsonl.1`
  - `events.jsonl.1` becomes `events.jsonl.2`
  - backups beyond `backup_count` are deleted
- If `backup_count` is `0`, the active log is truncated on rotation.
- `CtpRealtimeEngine.enable_event_log(...)` exposes the same options.

## CLI

Rotate JSONL event logs at roughly 1 MB and keep 5 backups:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --event-log-path output/ctp_events.jsonl `
  --event-log-max-bytes 1048576 `
  --event-log-backups 5
```

## Current Limitations

- Rotation is size-based only; date-based rotation is not implemented yet.
- Rotation happens synchronously in the event recording path.
- The framework does not compress rotated backups.
