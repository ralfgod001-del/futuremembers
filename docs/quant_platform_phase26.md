# Quant Platform Phase 26

Phase 26 adds strategy state schema versions and migration hooks.

## New Capabilities

- `Strategy` now has `state_schema_version`, defaulting to `1`.
- Runtime state JSON persists `strategy.state_schema_version`.
- `CtpRealtimeEngine.load_state(...)` compares the saved strategy state version
  with the current strategy version.
- If versions differ, the engine calls:

```python
def migrate_state(self, state, from_version):
    ...
```

- The migrated state is then passed into `restore_state(...)`.
- Successful migrations emit `STRATEGY_STATE_MIGRATED`.
- Loading newer state with the default strategy implementation raises a clear
  error, so accidental forward-incompatible restores do not proceed silently.

## Strategy Contract

```python
class MyStrategy(Strategy):
    state_schema_version = 2

    def migrate_state(self, state, from_version):
        migrated = dict(state)
        if from_version <= 1:
            migrated["has_submitted"] = bool(migrated.pop("submitted", False))
        return migrated
```

The migration hook should return JSON-safe state matching the current
`state_schema_version`.

## Current Limitations

- Migration is strategy-local; there is not yet a centralized migration registry.
- Realtime event log persistence is covered in Phase 27.
- Failed migrations raise exceptions and stop state loading.
