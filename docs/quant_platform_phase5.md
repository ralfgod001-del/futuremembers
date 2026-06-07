# Quant Platform Phase 5

Phase 5 adds the first futures-grade account layer while keeping the earlier
cash-style backtest path compatible.

## New Capabilities

- Contract specs with exchange, multiplier, tick size, margin rate, and commission rule.
- Futures account mode enabled by config: `"account": {"mode": "futures"}`.
- Futures equity curve columns: `margin`, `available`, `risk_ratio`, `unrealized_pnl`.
- Order metadata: `order_type`, `limit_price`, and `offset`.
- Futures positions exported to `futures_positions.csv`.
- Multiplier-adjusted realized and unrealized PnL.
- Limit order matching support in addition to next-open market order matching.

## Generate Futures Sample Data

```powershell
python -m quant_platform generate-sample --symbol RB2405 --output data/futures_rb2405.csv --periods 260
```

## Run Futures Backtest

```powershell
python -m quant_platform backtest --config examples/futures_config.json
```

## Config Shape

```json
{
  "account": {"mode": "futures"},
  "contracts": {
    "RB2405": {
      "exchange": "SHFE",
      "multiplier": 10,
      "tick_size": 1,
      "margin_rate": 0.12,
      "commission": {
        "rate": 0.0001,
        "min_commission": 1
      }
    }
  }
}
```

The follow-up phase adds today/yesterday position splits and daily settlement.
Exchange-specific settlement-price files and full CTP flag mapping remain future
hardening steps.
