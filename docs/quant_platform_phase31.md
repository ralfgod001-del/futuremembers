# Quant Platform Phase 31

Phase 31 adds AkShare historical futures data support.

## New Capabilities

- `load_akshare_futures_bars(...)` fetches AkShare futures history and maps it
  into the platform `Bar` model.
- Supported AkShare APIs:
  - `futures_zh_daily_sina`
  - `futures_main_sina`
  - `futures_hist_em`
  - `get_futures_daily`
- Config data sources now support `provider: akshare`.
- Optional `cache_path` stores fetched AkShare data as local CSV.
- New CLI command:

```powershell
python -m quant_platform data-akshare `
  --symbol RB0 `
  --start-date 20240101 `
  --end-date 20241231 `
  --output data/akshare_rb0_daily.csv
```

## Example Backtest

`examples/akshare_rb0_config.json` uses AkShare螺纹主连日线:

```powershell
python -m quant_platform backtest --config examples/akshare_rb0_config.json
```

The example writes:

- `data/akshare_rb0_daily.csv`
- `output/backtests/akshare_rb0/report.html`

## Config Example

```json
{
  "data": {
    "provider": "akshare",
    "api": "futures_zh_daily_sina",
    "symbol": "RB0",
    "start_date": "2024-01-01",
    "end_date": "2024-12-31",
    "cache_path": "data/akshare_rb0_daily.csv"
  }
}
```

## Current Limitations

- AkShare is used for historical testing only; realtime trading still uses CTP.
- AkShare field names can change, so the adapter keeps alias mapping but should
  be verified when upgrading AkShare.
- External data fetching depends on network availability and upstream service
  stability.
