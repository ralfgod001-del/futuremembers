# Quant Platform Phase 36

Phase 36 upgrades the Web K-line chart into a more useful analysis surface.

## New Capabilities

- Adds configurable moving average overlays:
  - `MA1`, `MA2`, and `MA3` can be enabled or disabled.
  - Default periods are 5, 20, and 60.
  - Period inputs update the chart immediately without reloading data.
- Adds a live chart legend showing the current MA values.
- Adds mouse hover inspection on the K-line canvas:
  - The chart draws a crosshair over the selected bar.
  - The info strip shows symbol, time, OHLC, percent change, volume, and MA
    values for the selected bar.
- Keeps the existing AkShare watchlist workflow unchanged.

## Notes

- Indicator rendering is currently frontend-side because the browser already has
  the full bar list from `/api/akshare-bars`.
- Later phases can add more indicators, drawing tools, and realtime CTP bar
  updates on the same chart surface.
