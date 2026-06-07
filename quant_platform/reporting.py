from __future__ import annotations

import json
from html import escape
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from .backtest import BacktestResult
    from .models import Trade


def calculate_metrics(
    equity_curve: pd.DataFrame,
    trades: list["Trade"],
    initial_cash: float,
) -> dict[str, float]:
    if equity_curve.empty:
        return {}

    equity = equity_curve["equity"].astype(float)
    returns = equity.pct_change().dropna()
    final_equity = float(equity.iloc[-1])
    total_return = final_equity / initial_cash - 1.0

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_drawdown = float(drawdown.min())

    sharpe = 0.0
    if len(returns) > 1 and float(returns.std()) != 0.0:
        sharpe = float(returns.mean() / returns.std() * (252**0.5))

    closing_pnls = [trade.realized_pnl for trade in trades if abs(trade.realized_pnl) > 1e-12]
    winners = [pnl for pnl in closing_pnls if pnl > 0]
    losers = [pnl for pnl in closing_pnls if pnl < 0]
    gross_profit = float(sum(winners))
    gross_loss = float(sum(losers))
    win_rate = float(len(winners) / len(closing_pnls)) if closing_pnls else 0.0
    profit_factor = abs(gross_profit / gross_loss) if gross_loss else 0.0

    return {
        "initial_cash": float(initial_cash),
        "final_equity": final_equity,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "trade_count": float(len(trades)),
        "closed_trade_count": float(len(closing_pnls)),
        "win_rate": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": float(profit_factor),
    }


def export_backtest_report(result: "BacktestResult", output_dir: str | Path) -> dict[str, Path]:
    report_dir = Path(output_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    equity_path = report_dir / "equity_curve.csv"
    orders_path = report_dir / "orders.csv"
    trades_path = report_dir / "trades.csv"
    positions_path = report_dir / "positions.csv"
    futures_positions_path = report_dir / "futures_positions.csv"
    events_path = report_dir / "event_log.csv"
    summary_path = report_dir / "summary.json"
    html_path = report_dir / "report.html"

    result.equity_curve.to_csv(equity_path, index=False)
    pd.DataFrame([asdict(order) for order in result.orders]).to_csv(orders_path, index=False)
    pd.DataFrame([asdict(trade) for trade in result.trades]).to_csv(trades_path, index=False)
    pd.DataFrame([asdict(position) for position in result.positions.values()]).to_csv(
        positions_path,
        index=False,
    )
    pd.DataFrame(
        [asdict(position) for position in result.futures_positions.values()]
    ).to_csv(
        futures_positions_path,
        index=False,
    )
    pd.DataFrame([_event_to_row(event) for event in result.events]).to_csv(
        events_path,
        index=False,
    )
    summary_path.write_text(json.dumps(result.metrics, indent=2), encoding="utf-8")
    html_path.write_text(render_html_report(result), encoding="utf-8")

    return {
        "equity_curve": equity_path,
        "orders": orders_path,
        "trades": trades_path,
        "positions": positions_path,
        "futures_positions": futures_positions_path,
        "events": events_path,
        "summary": summary_path,
        "html": html_path,
    }


def render_html_report(result: "BacktestResult") -> str:
    equity = result.equity_curve["equity"].astype(float).tolist()
    drawdown = (
        result.equity_curve["equity"].astype(float)
        / result.equity_curve["equity"].astype(float).cummax()
        - 1.0
    ).tolist()
    metrics_html = "\n".join(
        _metric_card(label, value)
        for label, value in [
            ("Final Equity", result.metrics.get("final_equity", 0.0)),
            ("Total Return", result.metrics.get("total_return", 0.0)),
            ("Max Drawdown", result.metrics.get("max_drawdown", 0.0)),
            ("Sharpe", result.metrics.get("sharpe", 0.0)),
            ("Trades", result.metrics.get("trade_count", 0.0)),
            ("Win Rate", result.metrics.get("win_rate", 0.0)),
            ("Margin", result.metrics.get("final_margin", 0.0)),
            ("Available", result.metrics.get("final_available", 0.0)),
            ("Risk Ratio", result.metrics.get("final_risk_ratio", 0.0)),
            ("Settlement PnL", result.metrics.get("final_settlement_pnl", 0.0)),
        ]
    )
    trades_rows = "\n".join(
        "<tr>"
        f"<td>{escape(str(trade.timestamp))}</td>"
        f"<td>{escape(trade.symbol)}</td>"
        f"<td>{escape(trade.side.value)}</td>"
        f"<td>{trade.quantity:g}</td>"
        f"<td>{trade.price:.4f}</td>"
        f"<td>{trade.realized_pnl:.4f}</td>"
        "</tr>"
        for trade in result.trades[-20:]
    )
    if not trades_rows:
        trades_rows = "<tr><td colspan=\"6\">No trades</td></tr>"
    event_rows = "\n".join(
        "<tr>"
        f"<td>{escape(str(event.timestamp))}</td>"
        f"<td>{escape(event.event_type)}</td>"
        f"<td>{escape(event.severity)}</td>"
        f"<td>{escape(event.symbol or '')}</td>"
        f"<td>{escape(event.message)}</td>"
        "</tr>"
        for event in result.events[-30:]
    )
    if not event_rows:
        event_rows = "<tr><td colspan=\"5\">No events</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Backtest Report</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #667085;
      --line: #d0d5dd;
      --bg: #f6f8fa;
      --panel: #ffffff;
      --accent: #1769aa;
      --loss: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.5 Arial, sans-serif;
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 28px auto 40px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: end;
      margin-bottom: 20px;
    }}
    h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; letter-spacing: 0; }}
    .muted {{ color: var(--muted); }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .metric, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .metric {{ padding: 14px; }}
    .metric .label {{ color: var(--muted); font-size: 12px; }}
    .metric .value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-bottom: 14px;
    }}
    section {{ padding: 16px; overflow: hidden; }}
    svg {{ width: 100%; height: 220px; display: block; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 8px 6px;
      text-align: right;
      white-space: nowrap;
    }}
    th:first-child, td:first-child,
    th:nth-child(2), td:nth-child(2),
    th:nth-child(3), td:nth-child(3) {{ text-align: left; }}
    @media (max-width: 760px) {{
      header, .grid {{ display: block; }}
      section {{ margin-bottom: 14px; }}
      h1 {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Backtest Report</h1>
        <div class="muted">Generated by quant_platform</div>
      </div>
      <div class="muted">Initial cash: {result.initial_cash:,.2f}</div>
    </header>
    <div class="metrics">
      {metrics_html}
    </div>
    <div class="grid">
      <section>
        <h2>Equity Curve</h2>
        {_line_svg(equity, "#1769aa")}
      </section>
      <section>
        <h2>Drawdown</h2>
        {_line_svg(drawdown, "#b42318")}
      </section>
    </div>
    <section>
      <h2>Recent Trades</h2>
      <table>
        <thead>
          <tr>
            <th>Time</th><th>Symbol</th><th>Side</th>
            <th>Qty</th><th>Price</th><th>Realized PnL</th>
          </tr>
        </thead>
        <tbody>{trades_rows}</tbody>
      </table>
    </section>
    <section>
      <h2>Event Log</h2>
      <table>
        <thead>
          <tr><th>Time</th><th>Type</th><th>Level</th><th>Symbol</th><th>Message</th></tr>
        </thead>
        <tbody>{event_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""


def _event_to_row(event: object) -> dict[str, object]:
    payload = getattr(event, "payload", {})
    return {
        "timestamp": getattr(event, "timestamp", None),
        "event_type": getattr(event, "event_type", None),
        "severity": getattr(event, "severity", None),
        "symbol": getattr(event, "symbol", None),
        "order_id": getattr(event, "order_id", None),
        "trade_id": getattr(event, "trade_id", None),
        "message": getattr(event, "message", None),
        "payload": json.dumps(payload, sort_keys=True),
    }


def _metric_card(label: str, value: float) -> str:
    return (
        "<div class=\"metric\">"
        f"<div class=\"label\">{escape(label)}</div>"
        f"<div class=\"value\">{escape(_format_metric(label, value))}</div>"
        "</div>"
    )


def _format_metric(label: str, value: float) -> str:
    if label in {"Total Return", "Max Drawdown", "Win Rate", "Risk Ratio"}:
        return f"{value:.2%}"
    if label == "Trades":
        return f"{int(value)}"
    if label == "Sharpe":
        return f"{value:.2f}"
    return f"{value:,.2f}"


def _line_svg(values: list[float], color: str) -> str:
    if not values:
        return "<svg viewBox=\"0 0 640 220\" role=\"img\"></svg>"

    width = 640
    height = 220
    pad = 18
    low = min(values)
    high = max(values)
    span = high - low or 1.0
    x_step = (width - pad * 2) / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = pad + index * x_step
        y = height - pad - ((value - low) / span) * (height - pad * 2)
        points.append(f"{x:.2f},{y:.2f}")
    polyline = " ".join(points)
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\">"
        f"<line x1=\"{pad}\" y1=\"{height - pad}\" x2=\"{width - pad}\" "
        f"y2=\"{height - pad}\" stroke=\"#d0d5dd\"/>"
        f"<polyline fill=\"none\" stroke=\"{color}\" stroke-width=\"2.5\" "
        f"points=\"{polyline}\"/>"
        "</svg>"
    )
