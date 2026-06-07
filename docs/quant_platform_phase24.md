# Quant Platform Phase 24

Phase 24 adds runtime state persistence for the CTP realtime engine.

## New Capabilities

- `CtpRealtimeEngine.runtime_state()` builds a JSON-safe state payload.
- `CtpRealtimeEngine.save_state(path)` persists:
  - local orders
  - local trades
  - last ticks
  - local order to CTP `OrderRef` mappings
  - cancel request mappings
  - filled quantity and notional accumulators
  - gateway request id
  - next CTP order ref
  - watchdog snapshot
  - latest reconciliation result
- `CtpRealtimeEngine.load_state(path)` restores the same runtime context.
- Restored engines continue local order ids from the previous maximum, so the
  next local order after `L00000001` becomes `L00000002`.
- Restored CTP order refs continue from the persisted gateway counter, avoiding
  reuse of a previous `OrderRef`.

## CLI

Save realtime state:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --save-state `
  --state-path output/ctp_realtime_state.json
```

Load realtime state before starting:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --load-state `
  --state-path output/ctp_realtime_state.json
```

## Current Limitations

- State is a local JSON file, not a transactional database.
- Strategy-specific private state persistence is covered in Phase 25.
- The persisted watchdog snapshot is informational; watchdog retry counters are
  rebuilt when a new process starts.
