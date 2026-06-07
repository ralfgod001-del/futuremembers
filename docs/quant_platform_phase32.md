# Quant Platform Phase 32

Phase 32 adds a Web-driven AkShare backtest workflow.

## New Capabilities

- New Web API: `POST /api/akshare-run`
- The API fetches AkShare futures history, caches the bars as CSV, runs a
  futures backtest, and returns the same report payload as normal Web runs.
- The Web sidebar now includes an AkShare Backtest section:
  - symbol
  - AkShare API
  - start date
  - end date
  - moving-average fast/slow windows
- AkShare Web runs appear in Recent Runs and reuse the existing metrics,
  equity curve, report, orders, trades, and event tables.

## Default UI Setup

- Symbol: `RB0`
- API: `futures_zh_daily_sina`
- Start: `2024-01-01`
- End: `2024-12-31`
- Strategy: sample moving average cross
- Account mode: futures
- Multiplier: `10`
- Margin rate: `0.12`

## API Example

```json
{
  "symbol": "RB0",
  "api": "futures_zh_daily_sina",
  "startDate": "20240101",
  "endDate": "20241231",
  "fastWindow": 5,
  "slowWindow": 20
}
```

The generated bar cache is written under:

```text
data/akshare_web/
```

## Current Limitations

- The UI currently exposes only the sample moving-average strategy.
- Advanced futures contract settings use sensible defaults in the UI; custom
  multiplier, commission, and margin editing can be added next.
- AkShare fetching is synchronous during the Web request, so slow upstream
  responses will keep the Run button busy until the request completes.
