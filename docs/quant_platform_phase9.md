# Quant Platform Phase 9

Phase 9 starts the futures-only CTP counter layer. It intentionally does not add
securities broker support.

## New Capabilities

- `CtpFuturesGateway` builds CTP futures `ReqOrderInsert` fields.
- `CtpConnectionConfig` stores CTP login/front metadata without hard-coding
  secrets.
- Local `Side`, `OrderType`, and `Offset` values map to CTP direction, price
  type, and offset flags.
- `Offset.AUTO` can be split into CTP-compatible order instructions.
- SHFE and INE auto-close behavior splits today positions before yesterday
  positions, then opens any remaining quantity.
- CTP trading account rows map into account snapshots.
- CTP investor position rows aggregate into `FuturesPosition` today/yesterday
  long/short buckets.
- CTP order and trade callbacks can be converted into local `Order` and `Trade`
  models.

## Dry Run

Build a CTP order insert request without sending it:

```powershell
python -m quant_platform ctp-order `
  --config examples/ctp_futures_config.json `
  --symbol RB2405 `
  --side buy `
  --quantity 2 `
  --order-type limit `
  --limit-price 3600 `
  --offset open
```

The command prints the exact request dictionary that a future CTP transport
will submit through `ReqOrderInsert`.

## Important Scope

This phase is the adapter and field-mapping layer. It does not load a CTP
dynamic library, connect to a real front, authenticate, or send live orders yet.

That separation lets the platform test all local-to-CTP semantics before a
native CTP binding is introduced.

## Current Limitations

- Partial fills are represented by CTP raw status, but local order status still
  uses the platform's simpler `PENDING` / `FILLED` / `CANCELED` states.
- Phase 10 adds the runtime session and optional Python CTP transport skeleton.
- CTP error callbacks and settlement confirmation workflow are not wired yet.
- Product eligibility, exchange-specific order restrictions, and night-session
  operational checks remain future work.
