from __future__ import annotations

import argparse
import csv
import json
import mimetypes
from collections import Counter, deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .backtest import BacktestEngine
from .cli import parse_strategy, resolve_backtest_setup
from .data import load_akshare_futures_bars, write_bars_csv
from .execution import ExecutionConfig
from .futures import ContractRegistry
from .optimization import run_grid_search
from .paper import run_paper_trading
from .replay import run_market_replay
from .risk import RiskManager


WEB_ROOT = Path(__file__).with_name("web_assets")
DEFAULT_CTP_STATE_PATH = "output/ctp_realtime_state.json"
DEFAULT_CTP_EVENT_LOG_PATH = "output/ctp_events.jsonl"
DEFAULT_CTP_STALE_SECONDS = 120.0


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    workspace: str | Path | None = None,
) -> None:
    root = Path(workspace or Path.cwd()).resolve()
    handler = build_handler(root)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"quant workspace: http://{host}:{port}")
    print(f"workspace root: {root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping quant workspace")
    finally:
        server.server_close()


def build_handler(workspace: Path) -> type[BaseHTTPRequestHandler]:
    class QuantWebHandler(BaseHTTPRequestHandler):
        server_version = "QuantWorkspace/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._send_asset(WEB_ROOT / "index.html")
                elif parsed.path.startswith("/assets/"):
                    asset_name = parsed.path.removeprefix("/assets/")
                    self._send_asset(WEB_ROOT / asset_name)
                elif parsed.path == "/api/configs":
                    self._send_json({"configs": list_config_files(workspace)})
                elif parsed.path == "/api/runs":
                    self._send_json({"runs": list_runs(workspace)})
                elif parsed.path == "/api/report":
                    params = parse_qs(parsed.query)
                    output_dir = params.get("output_dir", [""])[0]
                    self._send_json(load_run_report(workspace, output_dir))
                elif parsed.path == "/api/ctp-monitor":
                    params = parse_qs(parsed.query)
                    limit = int(params.get("limit", ["80"])[0] or 80)
                    stale_seconds = float(
                        params.get("stale_seconds", [str(DEFAULT_CTP_STALE_SECONDS)])[0]
                        or DEFAULT_CTP_STALE_SECONDS
                    )
                    self._send_json(
                        load_ctp_monitor(
                            workspace,
                            state_path=params.get("state_path", [None])[0],
                            event_log_path=params.get("event_log_path", [None])[0],
                            limit=limit,
                            stale_seconds=stale_seconds,
                        )
                    )
                elif parsed.path == "/api/akshare-bars":
                    self._send_json(load_akshare_bars(workspace, parse_qs(parsed.query)))
                elif parsed.path == "/report":
                    params = parse_qs(parsed.query)
                    output_dir = params.get("output_dir", [""])[0]
                    report_path = safe_workspace_path(workspace, output_dir) / "report.html"
                    self._send_file(report_path, inline=True)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "not found")
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/run":
                    payload = self._read_json()
                    self._send_json(run_quant_task(workspace, payload))
                    return
                if parsed.path == "/api/akshare-run":
                    payload = self._read_json()
                    self._send_json(run_akshare_backtest(workspace, payload))
                    return
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "not found")
                    return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}")

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            return json.loads(raw or "{}")

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_asset(self, path: Path) -> None:
            self._send_file(path, inline=True)

        def _send_file(self, path: Path, inline: bool = False) -> None:
            if not path.exists() or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "file not found")
                return
            content = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {
                "application/javascript",
                "application/json",
            }:
                content_type = f"{content_type}; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            if inline:
                self.send_header("Content-Disposition", "inline")
            self.end_headers()
            self.wfile.write(content)

    return QuantWebHandler


def list_config_files(workspace: Path) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for path in sorted((workspace / "examples").glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        configs.append(
            {
                "name": path.stem,
                "path": path.relative_to(workspace).as_posix(),
                "strategy": raw.get("strategy", ""),
                "modeHint": raw.get(
                    "mode",
                    "optimize" if "optimization" in raw else "backtest",
                ),
                "hasRisk": "risk" in raw,
                "hasOptimization": "optimization" in raw,
            }
        )
    return configs


def list_runs(workspace: Path) -> list[dict[str, Any]]:
    runs_root = workspace / "output" / "web_runs"
    if not runs_root.exists():
        return []

    runs: list[dict[str, Any]] = []
    for path in sorted(runs_root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        report = load_run_report(workspace, path.relative_to(workspace).as_posix(), limit=5)
        runs.append(
            {
                "name": path.name,
                "outputDir": path.relative_to(workspace).as_posix(),
                "metrics": report.get("metrics", {}),
                "mode": report.get("mode", ""),
            }
        )
    return runs


def run_quant_task(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode", "backtest"))
    if mode not in {"backtest", "replay", "paper", "optimize"}:
        raise ValueError("mode must be backtest, replay, paper, or optimize")

    config_path = safe_workspace_path(workspace, str(payload.get("configPath", "")))
    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_dir = workspace / "output" / "web_runs" / f"{mode}_{config_path.stem}_{timestamp}"
    max_steps = payload.get("maxSteps")

    args = SimpleNamespace(
        config=str(config_path),
        data=None,
        symbol=None,
        strategy=None,
        params=None,
        cash=None,
        commission_rate=None,
        slippage=None,
        output=str(output_dir),
    )
    setup = resolve_backtest_setup(args, default_output=str(output_dir))

    if mode == "backtest":
        strategy = parse_strategy(setup["strategy"], setup["params"])
        engine = BacktestEngine(
            bars=setup["bars"],
            strategy=strategy,
            initial_cash=setup["cash"],
            commission_rate=setup["commission_rate"],
            slippage=setup["slippage"],
            execution_config=setup["execution_config"],
            risk_manager=setup["risk_manager"],
            account_mode=setup["account_mode"],
            contract_registry=setup["contract_registry"],
            daily_settlement=setup["daily_settlement"],
        )
        result = engine.run()
        result.export(output_dir)
        metadata = {"mode": mode, "strategy": setup["strategy"]}
    elif mode == "replay":
        strategy = parse_strategy(setup["strategy"], setup["params"])
        replay = run_market_replay(
            bars=setup["bars"],
            strategy=strategy,
            initial_cash=setup["cash"],
            execution_config=setup["execution_config"],
            risk_manager=setup["risk_manager"],
            account_mode=setup["account_mode"],
            contract_registry=setup["contract_registry"],
            daily_settlement=setup["daily_settlement"],
            max_steps=int(max_steps) if max_steps else None,
        )
        replay.export(output_dir)
        metadata = {"mode": mode, "strategy": setup["strategy"], "steps": replay.steps}
    elif mode == "paper":
        strategy = parse_strategy(setup["strategy"], setup["params"])
        result = run_paper_trading(
            bars=setup["bars"],
            strategy=strategy,
            initial_cash=setup["cash"],
            execution_config=setup["execution_config"],
            risk_manager=setup["risk_manager"],
            account_mode=setup["account_mode"],
            contract_registry=setup["contract_registry"],
            daily_settlement=setup["daily_settlement"],
            max_steps=int(max_steps) if max_steps else None,
        )
        result.export(output_dir)
        metadata = {
            "mode": mode,
            "strategy": setup["strategy"],
            "steps": result.steps,
            "gateway": result.gateway,
        }
    else:
        config = setup["config"]
        optimization_config = config.get("optimization", {})
        grid = optimization_config.get("grid")
        if not grid:
            raise ValueError("optimize mode requires optimization.grid in config")
        objective = optimization_config.get("objective", "sharpe")

        def strategy_factory(params: dict[str, Any]) -> Any:
            return parse_strategy(setup["strategy"], params)

        search = run_grid_search(
            bars=setup["bars"],
            strategy_factory=strategy_factory,
            param_grid=grid,
            base_params=setup["params"],
            initial_cash=setup["cash"],
            execution_config=setup["execution_config"],
            risk_manager=setup["risk_manager"],
            account_mode=setup["account_mode"],
            contract_registry=setup["contract_registry"],
            daily_settlement=setup["daily_settlement"],
            commission_rate=setup["commission_rate"],
            slippage=setup["slippage"],
            objective=objective,
        )
        search.export(output_dir)
        metadata = {
            "mode": mode,
            "strategy": setup["strategy"],
            "objective": objective,
            "bestParams": search.best_params,
        }

    write_metadata(output_dir, metadata)
    report = load_run_report(workspace, output_dir.relative_to(workspace).as_posix())
    report.update(metadata)
    return report


def run_akshare_backtest(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    api_name = str(payload.get("api") or "futures_zh_daily_sina")
    start_date = str(payload.get("startDate") or payload.get("start_date") or "").strip() or None
    end_date = str(payload.get("endDate") or payload.get("end_date") or "").strip() or None
    output_symbol = str(payload.get("outputSymbol") or payload.get("output_symbol") or symbol)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_symbol = _safe_filename(output_symbol)
    output_dir = workspace / "output" / "web_runs" / f"akshare_{safe_symbol}_{timestamp}"
    cache_path = workspace / "data" / "akshare_web" / (
        f"{safe_symbol}_{_safe_filename(api_name)}_"
        f"{_safe_filename(start_date or 'all')}_{_safe_filename(end_date or 'latest')}.csv"
    )

    bars = load_akshare_futures_bars(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        api=api_name,
        market=payload.get("market") or None,
        variety=payload.get("variety") or None,
        period=str(payload.get("period") or "daily"),
        output_symbol=output_symbol,
    )
    if not bars:
        raise ValueError("AkShare returned no bars for the selected request")
    write_bars_csv(bars, cache_path)

    params = {
        "fast_window": int(payload.get("fastWindow") or 5),
        "slow_window": int(payload.get("slowWindow") or 20),
        "quantity": float(payload.get("quantity") or 1),
    }
    strategy_path = str(payload.get("strategy") or "sample:ma_cross")
    strategy = parse_strategy(strategy_path, params)
    cash = float(payload.get("cash") or 100_000)
    commission_rate = float(payload.get("commissionRate") or payload.get("commission_rate") or 0.0)
    slippage = float(payload.get("slippage") or 0.0)
    account_mode = str(payload.get("accountMode") or payload.get("account_mode") or "futures")
    daily_settlement = bool(payload.get("dailySettlement", True))
    contract_registry = _contract_registry_from_akshare_payload(output_symbol, payload)

    engine = BacktestEngine(
        bars=bars,
        strategy=strategy,
        initial_cash=cash,
        commission_rate=commission_rate,
        slippage=slippage,
        execution_config=ExecutionConfig.from_legacy(commission_rate, slippage),
        risk_manager=RiskManager(),
        account_mode=account_mode,
        contract_registry=contract_registry,
        daily_settlement=daily_settlement,
    )
    result = engine.run()
    result.export(output_dir)
    metadata = {
        "mode": "backtest",
        "strategy": strategy_path,
        "dataSource": "akshare",
        "api": api_name,
        "symbol": symbol,
        "outputSymbol": output_symbol,
        "market": payload.get("market") or None,
        "variety": payload.get("variety") or None,
        "period": str(payload.get("period") or "daily"),
        "startDate": start_date,
        "endDate": end_date,
        "bars": len(bars),
        "cachePath": cache_path.relative_to(workspace).as_posix(),
        "fastWindow": params["fast_window"],
        "slowWindow": params["slow_window"],
        "quantity": params["quantity"],
        "cash": cash,
        "commissionRate": commission_rate,
        "slippage": slippage,
        "accountMode": account_mode,
        "dailySettlement": daily_settlement,
        "exchange": payload.get("exchange") or "SHFE",
        "multiplier": float(payload.get("multiplier") or 10),
        "marginRate": float(payload.get("marginRate") or payload.get("margin_rate") or 0.12),
    }
    write_metadata(output_dir, metadata)
    report = load_run_report(workspace, output_dir.relative_to(workspace).as_posix())
    report.update(metadata)
    return report


def load_akshare_bars(workspace: Path, params: dict[str, Any]) -> dict[str, Any]:
    symbol = str(_param_value(params, "symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    api_name = str(_param_value(params, "api") or "futures_zh_daily_sina")
    start_date = str(_param_value(params, "startDate", "start_date") or "").strip() or None
    end_date = str(_param_value(params, "endDate", "end_date") or "").strip() or None
    output_symbol = str(_param_value(params, "outputSymbol", "output_symbol") or symbol).strip()
    limit = _positive_int(_param_value(params, "limit"), 240)

    bars = load_akshare_futures_bars(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        api=api_name,
        market=_param_value(params, "market") or None,
        variety=_param_value(params, "variety") or None,
        period=str(_param_value(params, "period") or "daily"),
        output_symbol=output_symbol,
    )
    visible_bars = bars[-limit:] if limit > 0 else bars

    return {
        "dataSource": "akshare",
        "symbol": output_symbol,
        "sourceSymbol": symbol,
        "api": api_name,
        "market": _param_value(params, "market") or None,
        "variety": _param_value(params, "variety") or None,
        "period": str(_param_value(params, "period") or "daily"),
        "startDate": start_date,
        "endDate": end_date,
        "count": len(visible_bars),
        "totalCount": len(bars),
        "bars": [_bar_to_dict(bar) for bar in visible_bars],
    }


def load_run_report(workspace: Path, output_dir: str, limit: int = 80) -> dict[str, Any]:
    run_dir = safe_workspace_path(workspace, output_dir)
    metadata = read_json(run_dir / "metadata.json")
    metrics = read_json(run_dir / "summary.json")
    best = read_json(run_dir / "best_params.json")

    best_backtest = run_dir / "best_backtest"
    report_dir = best_backtest if best else run_dir
    if not metrics and best:
        metrics = best.get("metrics", {})

    return {
        "outputDir": run_dir.relative_to(workspace).as_posix(),
        "mode": metadata.get("mode", "optimize" if best else "backtest"),
        "strategy": metadata.get("strategy", ""),
        "metrics": metrics,
        "best": best,
        "equity": read_csv_rows(best_backtest / "equity_curve.csv" if best else run_dir / "equity_curve.csv", limit),
        "orders": read_csv_rows(best_backtest / "orders.csv" if best else run_dir / "orders.csv", limit),
        "trades": read_csv_rows(best_backtest / "trades.csv" if best else run_dir / "trades.csv", limit),
        "events": read_csv_rows(best_backtest / "event_log.csv" if best else run_dir / "event_log.csv", limit),
        "optimization": read_csv_rows(run_dir / "optimization_results.csv", limit),
        "reportUrl": f"/report?output_dir={report_dir.relative_to(workspace).as_posix()}",
    }


def load_ctp_monitor(
    workspace: Path,
    state_path: str | None = None,
    event_log_path: str | None = None,
    limit: int = 80,
    stale_seconds: float = DEFAULT_CTP_STALE_SECONDS,
) -> dict[str, Any]:
    row_limit = max(int(limit), 0)
    state_file = safe_workspace_path(workspace, state_path or DEFAULT_CTP_STATE_PATH)
    event_log_file = safe_workspace_path(
        workspace,
        event_log_path or DEFAULT_CTP_EVENT_LOG_PATH,
    )
    state_payload = read_json(state_file) if state_file.exists() else {}
    event_rows = read_jsonl_rows(event_log_file, row_limit)
    orders = _tail_list(state_payload.get("orders"), row_limit)
    trades = _tail_list(state_payload.get("trades"), row_limit)
    ticks = _ticks_from_state(state_payload)
    summary = summarize_ctp_state(
        state_payload,
        event_rows,
        state_exists=state_file.exists(),
        event_log_exists=event_log_file.exists(),
        stale_seconds=stale_seconds,
    )

    return {
        "statePath": state_file.relative_to(workspace).as_posix(),
        "eventLogPath": event_log_file.relative_to(workspace).as_posix(),
        "stateExists": state_file.exists(),
        "eventLogExists": event_log_file.exists(),
        "stateSize": state_file.stat().st_size if state_file.exists() else 0,
        "eventLogSize": event_log_file.stat().st_size if event_log_file.exists() else 0,
        "eventBackups": list_event_log_backups(workspace, event_log_file),
        "summary": summary,
        "orders": orders,
        "trades": trades,
        "ticks": ticks,
        "events": event_rows,
    }


def summarize_ctp_state(
    state_payload: dict[str, Any],
    event_rows: list[dict[str, Any]],
    state_exists: bool = True,
    event_log_exists: bool = True,
    stale_seconds: float = DEFAULT_CTP_STALE_SECONDS,
) -> dict[str, Any]:
    orders = _list_from_value(state_payload.get("orders"))
    trades = _list_from_value(state_payload.get("trades"))
    ticks = _ticks_from_state(state_payload)
    state_events = _list_from_value(state_payload.get("events"))
    status_counts = Counter(str(order.get("status", "")) for order in orders)
    working_order_count = sum(
        count
        for status, count in status_counts.items()
        if status in {"PENDING"}
    )
    symbols = sorted(
        {
            *(str(symbol) for symbol in ticks.keys()),
            *(str(order.get("symbol")) for order in orders if order.get("symbol")),
            *(str(trade.get("symbol")) for trade in trades if trade.get("symbol")),
            *(
                str(symbol)
                for symbol in _connection_payload(state_payload, "market_data").get(
                    "subscribed_symbols",
                    [],
                )
            ),
        }
    )
    last_event = event_rows[-1] if event_rows else (state_events[-1] if state_events else None)
    strategy = state_payload.get("strategy") if isinstance(state_payload.get("strategy"), dict) else {}
    trading = _connection_payload(state_payload, "trading")
    market_data = _connection_payload(state_payload, "market_data")
    state_age_seconds = _age_seconds(state_payload.get("saved_at"))
    alerts = build_ctp_monitor_alerts(
        state_exists=state_exists,
        event_log_exists=event_log_exists,
        summary={
            "savedAt": state_payload.get("saved_at"),
            "stateAgeSeconds": state_age_seconds,
            "orderStatusCounts": dict(sorted(status_counts.items())),
            "rejectedOrderCount": status_counts.get("REJECTED", 0),
            "lastTickAt": _max_timestamp(ticks.values()),
            "trading": trading,
            "marketData": market_data,
        },
        event_rows=event_rows,
        stale_seconds=stale_seconds,
    )

    return {
        "healthStatus": _health_status(alerts),
        "alerts": alerts,
        "schemaVersion": state_payload.get("schema_version"),
        "savedAt": state_payload.get("saved_at"),
        "stateAgeSeconds": state_age_seconds,
        "staleSeconds": stale_seconds,
        "currentTime": state_payload.get("current_time"),
        "strategyName": strategy.get("name") or "",
        "strategyClass": strategy.get("class") or "",
        "strategyStateSchemaVersion": strategy.get("state_schema_version"),
        "orderCount": len(orders),
        "tradeCount": len(trades),
        "tickCount": len(ticks),
        "eventCount": len(event_rows),
        "stateEventCount": len(state_events),
        "workingOrderCount": working_order_count,
        "rejectedOrderCount": status_counts.get("REJECTED", 0),
        "orderStatusCounts": dict(sorted(status_counts.items())),
        "tradeQuantity": sum(_float_value(trade.get("quantity")) for trade in trades),
        "tradeNotional": sum(_float_value(trade.get("notional")) for trade in trades),
        "symbols": symbols,
        "lastTickAt": _max_timestamp(ticks.values()),
        "lastEvent": last_event,
        "trading": trading,
        "marketData": market_data,
        "watchdog": state_payload.get("watchdog") if isinstance(state_payload.get("watchdog"), dict) else {},
        "lastReconcile": state_payload.get("last_reconcile"),
    }


def build_ctp_monitor_alerts(
    state_exists: bool,
    event_log_exists: bool,
    summary: dict[str, Any],
    event_rows: list[dict[str, Any]],
    stale_seconds: float = DEFAULT_CTP_STALE_SECONDS,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if not state_exists:
        alerts.append(
            _alert(
                "ERROR",
                "STATE_MISSING",
                "Runtime state file is missing",
                "Run ctp-realtime with --save-state or check the state path.",
            )
        )
    elif summary.get("stateAgeSeconds") is not None and stale_seconds > 0:
        state_age = float(summary["stateAgeSeconds"])
        if state_age > stale_seconds:
            alerts.append(
                _alert(
                    "WARN",
                    "STATE_STALE",
                    "Runtime state is stale",
                    f"Last state save is {state_age:.0f}s old.",
                )
            )

    if not event_log_exists:
        alerts.append(
            _alert(
                "WARN",
                "EVENT_LOG_MISSING",
                "Event log file is missing",
                "Run ctp-realtime with --event-log-path to enable event monitoring.",
            )
        )
    elif not event_rows:
        alerts.append(
            _alert(
                "WARN",
                "EVENT_LOG_EMPTY",
                "Event log has no readable rows",
                "No recent JSONL events were found.",
            )
        )

    _append_connection_alerts(alerts, "TRADING", summary.get("trading"))
    _append_connection_alerts(alerts, "MARKET_DATA", summary.get("marketData"))

    market_data = summary.get("marketData") if isinstance(summary.get("marketData"), dict) else {}
    if market_data and not market_data.get("subscribed_symbols"):
        alerts.append(
            _alert(
                "WARN",
                "MARKET_DATA_NO_SUBSCRIPTIONS",
                "No market data subscriptions",
                "Market data is connected but no subscribed instruments were found.",
            )
        )
    if market_data.get("subscribed_symbols") and not summary.get("lastTickAt"):
        alerts.append(
            _alert(
                "WARN",
                "NO_TICK_RECEIVED",
                "No latest tick in state",
                "Subscribed instruments exist but no tick snapshot was saved.",
            )
        )

    rejected_count = int(summary.get("rejectedOrderCount") or 0)
    if rejected_count:
        alerts.append(
            _alert(
                "WARN",
                "ORDER_REJECTED",
                "Rejected orders detected",
                f"{rejected_count} rejected order(s) are present in runtime state.",
            )
        )

    for event in reversed(event_rows[-12:]):
        level = _event_alert_level(event)
        if level is None:
            continue
        alerts.append(
            _alert(
                level,
                str(event.get("event_type") or "EVENT_ALERT"),
                str(event.get("event_type") or "Runtime event needs attention"),
                str(event.get("message") or "Check the latest event payload."),
                source="event_log",
                timestamp=event.get("timestamp"),
            )
        )
        if len([alert for alert in alerts if alert.get("source") == "event_log"]) >= 5:
            break

    return alerts


def _append_connection_alerts(
    alerts: list[dict[str, Any]],
    name: str,
    payload: Any,
) -> None:
    if not isinstance(payload, dict) or not payload:
        return
    label = name.replace("_", " ").title()
    if payload.get("healthy") is False:
        alerts.append(
            _alert(
                "ERROR",
                f"{name}_UNHEALTHY",
                f"{label} is unhealthy",
                _connection_alert_message(payload),
            )
        )
        return
    if payload.get("connected") is False or payload.get("front_connected") is False:
        alerts.append(
            _alert(
                "ERROR",
                f"{name}_DISCONNECTED",
                f"{label} is disconnected",
                _connection_alert_message(payload),
            )
        )
        return
    if payload.get("logged_in") is False:
        alerts.append(
            _alert(
                "ERROR",
                f"{name}_NOT_LOGGED_IN",
                f"{label} is not logged in",
                _connection_alert_message(payload),
            )
        )


def _event_alert_level(event: dict[str, Any]) -> str | None:
    severity = str(event.get("severity") or "").upper()
    event_type = str(event.get("event_type") or "").upper()
    if severity in {"ERROR", "CRITICAL", "FATAL"}:
        return "ERROR"
    if "GIVE_UP" in event_type or event_type.endswith("_ERROR") or "ERROR" in event_type:
        return "ERROR"
    if any(token in event_type for token in ("REJECT", "TIMEOUT", "DISCONNECT", "BACKOFF")):
        return "WARN"
    if severity in {"WARN", "WARNING"}:
        return "WARN"
    return None


def _health_status(alerts: list[dict[str, Any]]) -> str:
    levels = {str(alert.get("level", "")).upper() for alert in alerts}
    if "ERROR" in levels:
        return "ERROR"
    if "WARN" in levels:
        return "WARN"
    return "OK"


def _alert(
    level: str,
    code: str,
    title: str,
    message: str,
    source: str = "monitor",
    timestamp: Any = None,
) -> dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "title": title,
        "message": message,
        "source": source,
        "timestamp": timestamp,
    }


def _connection_alert_message(payload: dict[str, Any]) -> str:
    parts = []
    if payload.get("state"):
        parts.append(f"state={payload['state']}")
    for key in ("connected", "front_connected", "logged_in", "authenticated", "settlement_confirmed"):
        if key in payload:
            parts.append(f"{key}={payload[key]}")
    if payload.get("last_disconnect_reason"):
        parts.append(f"reason={payload['last_disconnect_reason']}")
    return ", ".join(parts) or "Connection status is incomplete."


def read_jsonl_rows(path: Path, limit: int = 80) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists() or path.stat().st_size == 0:
        return []
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return list(rows)


def list_event_log_backups(workspace: Path, event_log_file: Path) -> list[dict[str, Any]]:
    backups: list[dict[str, Any]] = []
    if not event_log_file.parent.exists():
        return backups
    prefix = f"{event_log_file.name}."
    for path in sorted(event_log_file.parent.glob(f"{event_log_file.name}.*")):
        suffix = path.name.removeprefix(prefix)
        if not suffix.isdigit() or not path.is_file():
            continue
        backups.append(
            {
                "path": path.relative_to(workspace).as_posix(),
                "size": path.stat().st_size,
                "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            }
        )
    return backups


def safe_workspace_path(workspace: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("path is required")
    path = (workspace / unquote(raw_path)).resolve()
    if path != workspace and workspace not in path.parents:
        raise ValueError("path must stay inside workspace")
    return path


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_metadata(output_dir: Path, metadata: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_csv_rows(path: Path, limit: int = 80) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for index, row in enumerate(reader):
            if index >= limit:
                break
            rows.append({key: value for key, value in row.items()})
        return rows


def _tail_list(value: Any, limit: int) -> list[dict[str, Any]]:
    rows = _list_from_value(value)
    if limit <= 0:
        return []
    return rows[-limit:]


def _list_from_value(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _ticks_from_state(state_payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("last_ticks", "ticks"):
        ticks = state_payload.get(key)
        if isinstance(ticks, dict):
            return ticks
    market_data = state_payload.get("market_data")
    if isinstance(market_data, dict) and isinstance(market_data.get("ticks"), dict):
        return market_data["ticks"]
    return {}


def _connection_payload(state_payload: dict[str, Any], key: str) -> dict[str, Any]:
    direct = state_payload.get(key)
    if isinstance(direct, dict):
        return direct
    watchdog = state_payload.get("watchdog")
    if isinstance(watchdog, dict) and isinstance(watchdog.get(key), dict):
        return watchdog[key]
    return {}


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _age_seconds(value: Any) -> float | None:
    timestamp = _parse_datetime(value)
    if timestamp is None:
        return None
    now = datetime.now(timestamp.tzinfo) if timestamp.tzinfo else datetime.now()
    return max((now - timestamp).total_seconds(), 0.0)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _max_timestamp(rows: Any) -> str | None:
    timestamps = [
        str(row.get("timestamp"))
        for row in rows
        if isinstance(row, dict) and row.get("timestamp")
    ]
    return max(timestamps) if timestamps else None


def _param_value(params: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name not in params:
            continue
        value = params.get(name)
        if isinstance(value, list):
            value = value[0] if value else None
        if value not in (None, ""):
            return value
    return None


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _bar_to_dict(bar: Any) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _contract_registry_from_akshare_payload(symbol: str, payload: dict[str, Any]) -> ContractRegistry:
    return ContractRegistry.from_mapping(
        {
            symbol: {
                "exchange": payload.get("exchange") or "SHFE",
                "multiplier": float(payload.get("multiplier") or 10),
                "tick_size": float(payload.get("tickSize") or payload.get("tick_size") or 1),
                "margin_rate": float(payload.get("marginRate") or payload.get("margin_rate") or 0.12),
                "commission": {
                    "rate": float(payload.get("commissionRate") or payload.get("commission_rate") or 0.0),
                    "close_today_rate": float(
                        payload.get("closeTodayCommissionRate")
                        or payload.get("close_today_commission_rate")
                        or payload.get("commissionRate")
                        or payload.get("commission_rate")
                        or 0.0
                    ),
                    "per_contract": float(payload.get("commissionPerContract") or 0.0),
                    "close_today_per_contract": float(payload.get("closeTodayCommissionPerContract") or 0.0),
                    "min_commission": float(payload.get("minCommission") or 0.0),
                },
            }
        }
    )


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(value)) or "value"


def parse_serve_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local quant workspace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", default=".")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_serve_args(argv)
    run_server(host=args.host, port=args.port, workspace=args.workspace)


if __name__ == "__main__":
    main()
