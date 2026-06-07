# Quant Platform Phase 35

Phase 35 adds an AkShare-backed watchlist and K-line chart to the local Web
workspace.

## New Capabilities

- New Web API: `GET /api/akshare-bars`
  - Loads futures OHLCV bars through the existing AkShare data adapter.
  - Accepts `symbol`, `api`, `market`, `variety`, `period`, `startDate`,
    `endDate`, `outputSymbol`, and `limit`.
  - Returns chart-ready rows with `timestamp`, `open`, `high`, `low`, `close`,
    and `volume`.
- New sidebar watchlist:
  - Defaults to `RB0`, `IF0`, and `CU0`.
  - Supports adding, selecting, and removing contracts.
  - Saves the list in browser `localStorage`.
- New main workspace K-line chart:
  - Draws candlesticks and volume bars from AkShare history.
  - Uses red for rising bars and green for falling bars.
  - Re-fetches when the selected contract or AkShare data parameters change.

## Notes

- This phase still uses AkShare historical data for testing and research.
- Realtime K-line updates can later reuse the same chart surface with CTP tick
  aggregation from the live engine.
