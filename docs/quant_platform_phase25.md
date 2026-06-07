# Quant Platform Phase 25

Phase 25 adds strategy state persistence.

## New Capabilities

- `Strategy` now exposes optional state hooks:
  - `snapshot_state()`
  - `restore_state(state)`
- `CtpRealtimeEngine.runtime_state()` persists strategy metadata and strategy
  state inside the same JSON runtime state file.
- `CtpRealtimeEngine.load_state(...)` restores strategy state.
- If runtime state is loaded before `engine.start(...)`, the engine reapplies
  strategy state after `on_init(...)`, so initialization cannot accidentally
  wipe restored fields.
- `BuyFirstTickStrategy` now persists whether it has already submitted its first
  tick order.
- Restoring strategy state records a `STRATEGY_STATE_LOADED` engine event.

## Strategy Contract

Strategies that need persistence can implement:

```python
def snapshot_state(self):
    return {"has_submitted": self.has_submitted}

def restore_state(self, state):
    self.has_submitted = bool(state.get("has_submitted", False))
```

Returned state must be JSON-safe because it is written into the engine runtime
state file.

## CLI

The existing Phase 24 CLI flags now also include strategy state:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --save-state `
  --state-path output/ctp_realtime_state.json
```

## Current Limitations

- Strategy state is opt-in; strategies that do not override the hooks persist an
  empty state object.
- Strategy state schema versioning and migration are covered in Phase 26.
- Non-JSON Python objects must be converted by the strategy before returning
  from `snapshot_state()`.
