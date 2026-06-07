# Quant Platform Phase 4

Phase 4 adds a local web workspace for running and inspecting the platform without
typing every command by hand.

## Start The Workspace

```powershell
python -m quant_platform serve --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765
```

## Current Workspace Features

- Select a JSON config from `examples/`.
- Run backtest, replay, paper, or optimization jobs.
- Limit replay by timestamp count.
- View metrics, equity curve, orders, trades, events, and optimization rows.
- Open the generated HTML report.
- Reopen recent runs from `output/web_runs/`.

## API Endpoints

- `GET /api/configs`
- `GET /api/runs`
- `GET /api/report?output_dir=...`
- `POST /api/run`

Example payload:

```json
{
  "mode": "replay",
  "configPath": "examples/replay_risk_config.json",
  "maxSteps": 120
}
```

The web server intentionally uses Python's standard library. It is meant as a
local operator console first; a heavier frontend framework can come later once
the core workflows settle.
