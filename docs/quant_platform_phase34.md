# Quant Platform Phase 34

Phase 34 expands the Chinese AkShare Web backtest controls.

## New Capabilities

- AkShare Web backtest now exposes more parameters in the sidebar:
  - backtest symbol name
  - exchange
  - AkShare market
  - variety filter
  - period
  - initial cash
  - order quantity
  - contract multiplier
  - margin rate
  - commission rate
  - slippage
- `POST /api/akshare-run` already accepted these values; this phase makes them
  editable from the UI.
- Web run metadata now records the AkShare request and futures contract
  parameters so results can be reviewed later.

## Notes

- The default remains `RB0`, `SHFE`, daily bars, 100000 cash, multiplier 10,
  and margin rate 0.12.
- `AkShare 市场` is mainly needed by `get_futures_daily`.
- `品种过滤` is optional and useful when an AkShare API returns a broader
  futures universe.
