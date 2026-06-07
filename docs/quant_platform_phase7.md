# Quant Platform Phase 7

Phase 7 hardens the futures account toward domestic futures behavior.

## New Capabilities

- Split futures positions into:
  - long today
  - long yesterday
  - short today
  - short yesterday
- Enforce close availability for `CLOSE_TODAY`, `CLOSE_YESTERDAY`, and `CLOSE`.
- Apply close-today commission rules when the fill closes today positions.
- Optional daily settlement via `"account": {"daily_settlement": true}`.
- Daily settlement transfers mark-to-market PnL into cash, resets position cost to settlement price, and rolls today positions into yesterday positions.
- Settlement events are written to `event_log.csv`.

## Futures Account Config

```json
{
  "account": {
    "mode": "futures",
    "daily_settlement": true
  },
  "contracts": {
    "RB2405": {
      "exchange": "SHFE",
      "multiplier": 10,
      "tick_size": 1,
      "margin_rate": 0.12,
      "commission": {
        "rate": 0.0001,
        "close_today_rate": 0.0002,
        "per_contract": 0,
        "close_today_per_contract": 1.5,
        "min_commission": 1
      }
    }
  }
}
```

## Current Limitations

- Settlement price currently defaults to the latest bar close.
- Product-specific exchange calendars are still template based.
- Close-priority for `AUTO` currently closes today first, then yesterday, then opens the remaining quantity.
- CTP-specific order flags are represented conceptually, not yet mapped to broker API fields.
