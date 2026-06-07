# Quant Platform Phase 37

Phase 37 adds chart navigation to the Web K-line workspace.

## New Capabilities

- Adds K-line navigation buttons:
  - pan backward
  - zoom in
  - zoom out
  - pan forward
  - jump to latest
- The K-line note now shows the visible window range, for example
  `103-242/242`.
- Mouse wheel on the K-line chart zooms the visible window.
- Shift + mouse wheel pans through history.
- AkShare chart loading now requests a larger local window so historical
  browsing can happen without rerunning a backtest.

## Notes

- Navigation is frontend-side and uses the bars already returned by
  `/api/akshare-bars`.
- The backend data endpoint is unchanged.
- Later phases can add a mini overview chart, drag selection, or saved chart
  layouts.
