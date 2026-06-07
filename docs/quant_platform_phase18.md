# Quant Platform Phase 18

Phase 18 lets realtime CTP ticks drive bar-based strategies.

## New Capabilities

- `TickBarAggregator` converts normalized `Tick` objects into completed minute
  `Bar` objects.
- `CtpRealtimeEngine` accepts `bar_frequency="1min"`.
- Completed realtime bars are:
  - added to strategy history
  - recorded as `BAR` events
  - dispatched to `strategy.on_bar(context, bar)`
- `StrategyContext.history(...)` and `context.closes(...)` now work for bars
  generated from realtime ticks.
- Realtime snapshots include completed bars under `bars`.
- `ctp-realtime` supports:
  - `--bar-frequency 1min`
  - `--flush-bars`

## CLI

Run a realtime dry-run and flush the current bar into the snapshot:

```powershell
python -m quant_platform ctp-realtime `
  --config examples/ctp_futures_config.json `
  --skip-queries `
  --simulate-tick `
  --bar-frequency 1min `
  --flush-bars
```

## Bar Semantics

- The current implementation supports `1min`.
- Bar timestamp is the minute bucket start.
- Volume is based on cumulative CTP tick volume deltas; the first tick for a
  symbol contributes zero volume because no prior cumulative volume is known.
- The current in-progress bar is emitted when the next minute starts or when
  `flush_bars()` / `--flush-bars` is used.

## Current Limitations

- Only one-minute bars are implemented.
- Out-of-order ticks are not repaired.
- Session boundary resets for cumulative volume are not yet explicit.
- Phase 19 adds front connected/disconnected callback handling and session
  health fields.
- Automatic resubscribe/requery after reconnect is still future work.
