# Quant Platform Phase 3

Phase 3 starts turning the research engine into a paper-trading core:

- Risk checks before orders enter the pending queue.
- Event logging for run start, bars, submitted orders, rejected orders, fills, cancels, and run finish.
- Historical market replay with the same strategy and order lifecycle as backtests.
- Replay exports that can be inspected like an audit trail.

## Run A Replay

```powershell
python -m quant_platform replay --config examples/replay_risk_config.json --stream
```

Limit the replay to the first 80 timestamps:

```powershell
python -m quant_platform replay --config examples/replay_risk_config.json --max-steps 80 --output output/replays/phase3_80
```

Replay outputs:

- `event_log.csv`
- `summary.json`
- `equity_curve.csv`
- `orders.csv`
- `trades.csv`
- `positions.csv`
- `report.html`

## Risk Config

```json
{
  "risk": {
    "enabled": true,
    "max_drawdown": 0.1,
    "default": {
      "max_order_quantity": 2,
      "max_position_quantity": 4,
      "max_order_notional": 1000,
      "allow_short": false
    },
    "symbols": {
      "SAMPLE_A": {
        "max_order_quantity": 1
      }
    }
  }
}
```

The current risk layer is intentionally conservative and simple. It checks submitted
orders against quantity, projected position, notional exposure, short-selling rules,
and account drawdown. Later live gateways can reuse this same layer before sending
orders to a broker.
