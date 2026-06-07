from __future__ import annotations

import argparse
import html
import json
import threading
import time
from collections import OrderedDict
from datetime import date, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

import pandas as pd

from .adapters import CFFEXAdapter, SHFEAdapter
from .database import PositionsDatabase
from .market_data import build_cffex_settlement_frame, fetch_cffex_daily_market, fetch_daily_market
from .reports import (
    DEEPSEEK_DEFAULT_MODEL,
    build_ai_prompt,
    build_report_page,
    call_deepseek,
    extract_text,
    top5_daily_summary,
)
from .shfe_report import build_dashboard_html
from .utils import session


DEFAULT_DB = "data/shfe_positions.sqlite"
DEFAULT_DASHBOARD = "output/shfe_system/index.html"
DEFAULT_START_DATE = date(2024, 5, 20)



DEFAULT_REPORT_DB = "data/shfe_positions.sqlite"

# In-memory TTL cache for assembled reports so rapid clicks on "open report"
# or "save as HTML/JSON" do not re-run the ~30-60s DeepSeek call. The AI call
# is also serialized via a semaphore to avoid fanning out concurrent requests
# that could exhaust the API quota.
_REPORT_CACHE: "OrderedDict[tuple[str, int], tuple[float, dict, str | None, str | None]]" = OrderedDict()
_REPORT_CACHE_TTL = 600.0  # seconds (10 minutes)
_REPORT_CACHE_MAX = 32
_AI_SEMAPHORE = threading.Semaphore(1)
_REPORT_CACHE_LOCK = threading.Lock()


def _cached_report_data(db_path: str, product: str, days: int):
    """Return `(summary, ai_summary, ai_error)` for (product, days).

    Serves from the TTL cache when fresh; otherwise rebuilds, serializing the
    DeepSeek call. Aggregation errors propagate; AI errors are captured as
    `ai_error` (never raised) so a report still renders.
    """
    now = time.time()
    key = (product, days)
    with _REPORT_CACHE_LOCK:
        cached = _REPORT_CACHE.get(key)
        if cached is not None:
            ts, summary, ai_summary, ai_error = cached
            if now - ts < _REPORT_CACHE_TTL:
                _REPORT_CACHE.move_to_end(key)
                return summary, ai_summary, ai_error

    database = PositionsDatabase(db_path)
    summary = top5_daily_summary(database, product, days=days, top_n=5)
    prompt = build_ai_prompt(summary)
    ai_summary: str | None = None
    ai_error: str | None = None
    with _AI_SEMAPHORE:
        try:
            api_resp = call_deepseek(prompt)
            ai_summary = extract_text(api_resp)
        except Exception as exc:  # noqa: BLE001 - surface as report-level error
            ai_error = str(exc)
    with _REPORT_CACHE_LOCK:
        _REPORT_CACHE[key] = (time.time(), summary, ai_summary, ai_error)
        _REPORT_CACHE.move_to_end(key)
        while len(_REPORT_CACHE) > _REPORT_CACHE_MAX:
            _REPORT_CACHE.popitem(last=False)
    return summary, ai_summary, ai_error


def make_report_handler(serve_directory: str, db_path: str) -> type[SimpleHTTPRequestHandler]:
    """Build a HTTPRequestHandler that serves static files and a /report endpoint.

    The /report endpoint accepts query params:
      - product: product name (URL-encoded UTF-8)
      - days:    integer 5/10/15
      - ai:      "1" to also call DeepSeek and include "ai_summary" in the JSON
    Errors are returned as JSON with status 4xx/5xx.
    """

    class ReportHandler(SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Quieter logs; route to stdout without ANSI noise.
            print("[serve] " + fmt % args, flush=True)

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/report":
                return self._handle_report(parsed)
            if parsed.path == "/report/page":
                return self._handle_report_page(parsed)
            if parsed.path == "/report/download":
                return self._handle_report_download(parsed)
            return super().do_GET()

        def _handle_report_page(self, parsed) -> None:
            """Return a full standalone HTML report page."""
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            product = params.get("product") or ""
            try:
                days = int(params.get("days") or "5")
            except ValueError:
                days = 5
            if days not in (5, 10, 15):
                self._send_json(400, {"error": "days must be 5/10/15"})
                return
            if not product:
                body = "<h1>\u9519\u8bef\uff1a\u7f3a\u5c11 product \u53c2\u6570</h1>"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return

            try:
                summary, ai_summary, ai_error = _cached_report_data(db_path, product, days)
            except Exception as exc:
                body = f"<h1>\u805a\u5408\u5931\u8d25</h1><p>{html.escape(str(exc))}</p>"
                self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return

            page_html = build_report_page(summary, ai_summary=ai_summary, ai_error=ai_error)
            body = page_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _handle_report(self, parsed) -> None:
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            product = params.get("product") or ""
            try:
                days = int(params.get("days") or "5")
            except ValueError:
                days = 5
            if days not in (5, 10, 15):
                return self._send_json(400, {"error": "days must be 5/10/15"})
            if not product:
                return self._send_json(400, {"error": "product is required"})
            want_ai = params.get("ai") in ("1", "true", "yes")

            try:
                if want_ai:
                    summary, ai_summary, ai_error = _cached_report_data(db_path, product, days)
                else:
                    database = PositionsDatabase(db_path)
                    summary = top5_daily_summary(database, product, days=days, top_n=5)
                    ai_summary = None
                    ai_error = None
            except Exception as exc:
                return self._send_json(500, {"error": "aggregation failed", "detail": str(exc)})

            response = {"summary": summary, "ai_summary": ai_summary}
            if ai_error:
                response["ai_error"] = ai_error
            return self._send_json(200, response)


        def _parse_report_params(self, parsed):
            """Extract product/days. Returns (product, days, error_tuple_or_None)."""
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            product = params.get("product") or ""
            try:
                days = int(params.get("days") or "5")
            except ValueError:
                days = 5
            if days not in (5, 10, 15):
                return product, days, (400, "days must be 5/10/15")
            if not product:
                return product, days, (400, "product is required")
            return product, days, None

        def _build_report_data(self, product, days):
            """Build (summary, ai_summary, ai_error). Shared by page + download.

            Results come from the TTL cache and the DeepSeek call is serialized
            so rapid clicks on save/open do not fan out concurrent API calls.
            """
            return _cached_report_data(db_path, product, days)

        def _handle_report_download(self, parsed):
            """Download report as HTML or JSON with Content-Disposition attachment.

            GET /report/download?product=IF&days=5&format=html
            GET /report/download?product=IF&days=5&format=json
            """
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            fmt = (params.get("format") or "html").lower()
            if fmt not in ("html", "json"):
                return self._send_json(400, {"error": "format must be html or json"})
            product, days, err = self._parse_report_params(parsed)
            if err:
                return self._send_json(err[0], {"error": err[1]})

            try:
                summary, ai_summary, ai_error = self._build_report_data(product, days)
            except Exception as exc:
                return self._send_json(500, {"error": "aggregation failed", "detail": str(exc)})

            date_tag = datetime.now().strftime("%Y%m%d")
            safe_product = product.replace("/", "_").replace("\\", "_")

            if fmt == "html":
                page_html = build_report_page(summary, ai_summary=ai_summary, ai_error=ai_error)
                body = page_html.encode("utf-8")
                filename = f"{safe_product}_{days}d_{date_tag}.html"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                payload = {
                    "summary": summary,
                    "ai_summary": ai_summary,
                    "ai_error": ai_error,
                }
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                filename = f"{safe_product}_{days}d_{date_tag}.json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

    # SimpleHTTPRequestHandler uses the `directory` kwarg of __init__ (3.7+).
    # functools.partial binds it so each request opens in the right directory.
    from functools import partial
    return partial(ReportHandler, directory=serve_directory)

def parse_date(value: str | None, default: date) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else default


def shfe_trading_days() -> set[str] | None:
    try:
        from akshare.futures.cot import calendar
    except Exception:
        return None
    return set(calendar)


def update_incremental(
    database: PositionsDatabase,
    start_date: date,
    end_date: date,
    timeout: int = 30,
    pause_seconds: float = 0.05,
    adapters: list | None = None,
    http: requests.Session | None = None,
    trading_days: set[str] | None = None,
    adapter=None,
) -> dict:
    """Incrementally fetch positions for both SHFE and CFFEX.

    The previous single-adapter signature still works: when ``adapters`` is
    None we default to [SHFE(), CFFEX()]. Passing ``adapters=[SHFEAdapter()]``
    reproduces the legacy SHFE-only behavior (used by tests).
    """
    database.initialize()
    calendar = shfe_trading_days() if trading_days is None else trading_days
    if adapter is not None and adapters is None:
        adapters = [adapter]
    elif adapters is None:
        adapters = [SHFEAdapter(), CFFEXAdapter()]
    http = http or session(timeout)
    result = {
        "missing": 0,
        "downloaded": 0,
        "no_data": 0,
        "errors": 0,
        "rows": 0,
        "per_exchange": {},
    }

    # Compute a per-exchange missing-date list so CFFEX backfill is not
    # short-circuited by SHFE having already stored rows for that date.
    for adapter in adapters:
        ex = adapter.exchange
        missing_ex = database.missing_weekdays_for_exchange(
            start_date, end_date, exchange=ex, trading_days=calendar
        )
        result["per_exchange"][ex] = {
            "missing": len(missing_ex),
            "downloaded": 0,
            "no_data": 0,
            "errors": 0,
            "rows": 0,
        }
        per = result["per_exchange"][ex]
        result["missing"] += len(missing_ex)
        for trade_date in missing_ex:
            # Rate-limit: keep ≥1s between requests to be polite to CFFEX.
            if ex == "CFFEX" and pause_seconds < 1.0:
                pause = 1.0
            else:
                pause = pause_seconds
            source_url = getattr(adapter, "base_url", "")
            if source_url:
                try:
                    source_url = source_url.format(date=trade_date.strftime("%Y%m%d"))
                except Exception:
                    pass
            try:
                data = adapter.fetch(trade_date, http) if ex == "CFFEX" else adapter.fetch_official(trade_date, http)
                if data.normalized.empty:
                    per["no_data"] += 1
                else:
                    rows = database.upsert_frame(data.normalized, replace_trade_date=True)
                    per["downloaded"] += 1
                    per["rows"] += rows
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    per["no_data"] += 1
                else:
                    per["errors"] += 1
            except Exception:
                per["errors"] += 1
            if pause:
                time.sleep(pause)
        # Aggregate per_exchange tallies into the overall result.
        for k in ("downloaded", "no_data", "errors", "rows"):
            result[k] += per[k]
    return result


def update_market_incremental(
    database: PositionsDatabase,
    start_date: date,
    end_date: date,
    timeout: int = 30,
    pause_seconds: float = 0.05,
    http: requests.Session | None = None,
    trading_days: set[str] | None = None,
    fetcher=fetch_daily_market,
    fetch_cffex=fetch_cffex_daily_market,
    include_cffex: bool = True,
) -> dict:
    """Incrementally fetch daily market+settlement for SHFE, then CFFEX rtj.

    The original SHFE flow is unchanged. After SHFE completes we also pull
    CFFEX rtj (settlement+volume+OI) and write it into the same
    ``contract_daily_market`` table. CFFEX has no settlement-params feed, so
    the settlement frame is empty for the CFFEX half.
    """
    database.initialize()
    calendar = shfe_trading_days() if trading_days is None else trading_days
    http = http or session(timeout)
    result = {
        "missing": 0,
        "downloaded": 0,
        "no_data": 0,
        "errors": 0,
        "market_rows": 0,
        "settlement_rows": 0,
        "per_exchange": {"SHFE": {}, "CFFEX": {}},
    }

    # SHFE missing list — uses the original sync_status-aware query.
    missing_shfe = database.missing_market_days_for_exchange(
        start_date, end_date, exchange="SHFE", trading_days=calendar
    )
    result["per_exchange"]["SHFE"] = {
        "missing": len(missing_shfe),
        "downloaded": 0,
        "no_data": 0,
        "errors": 0,
        "market_rows": 0,
        "settlement_rows": 0,
    }
    result["missing"] += len(missing_shfe)

    # ---------- SHFE / INE half (legacy) ----------
    for trade_date in missing_shfe:
        try:
            market, settlement, market_url, settlement_url = fetcher(trade_date, http)
            if market.empty:
                database.mark_market_sync(
                    trade_date,
                    "no_data",
                    message="SHFE 官方接口无合约日行情",
                    source_url=market_url,
                )
                result["no_data"] += 1
            else:
                counts = database.upsert_market_day(market, settlement, replace_trade_date=True)
                database.mark_market_sync(
                    trade_date,
                    "ok",
                    market_rows=counts["market_rows"],
                    settlement_rows=counts["settlement_rows"],
                    source_url=f"{market_url} | {settlement_url}",
                )
                result["downloaded"] += 1
                result["market_rows"] += counts["market_rows"]
                result["settlement_rows"] += counts["settlement_rows"]
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                database.mark_market_sync(
                    trade_date,
                    "no_data",
                    message="SHFE 官方接口 404",
                )
                result["no_data"] += 1
            else:
                database.mark_market_sync(trade_date, "error", message=str(exc))
                result["errors"] += 1
        except Exception as exc:
            database.mark_market_sync(trade_date, "error", message=str(exc))
            result["errors"] += 1
        if pause_seconds:
            time.sleep(pause_seconds)

    # SHFE-only totals already live in result["per_exchange"]["SHFE"] above;
    # the running result counters also already include SHFE only.

    # ---------- CFFEX half ----------
    cffex_stat = {"downloaded": 0, "no_data": 0, "errors": 0, "market_rows": 0}
    if include_cffex:
        missing_cffex = database.missing_market_days_for_exchange(
            start_date, end_date, exchange="CFFEX", trading_days=calendar
        )
        result["per_exchange"]["CFFEX"] = {
            "missing": len(missing_cffex),
            "downloaded": 0,
            "no_data": 0,
            "errors": 0,
            "market_rows": 0,
        }
        result["missing"] += len(missing_cffex)
        for trade_date in missing_cffex:
            try:
                market, url = fetch_cffex(trade_date, http)
                if market.empty:
                    cffex_stat["no_data"] += 1
                else:
                    # CFFEX rtj has no margin feed, so synthesize settlement
                    # params from the standard exchange-level margin rates.
                    settlement = build_cffex_settlement_frame(market, source_url=url)
                    counts = database.upsert_market_day(market, settlement, replace_trade_date=True)
                    cffex_stat["downloaded"] += 1
                    cffex_stat["market_rows"] += counts["market_rows"]
                    cffex_stat.setdefault("settlement_rows", 0)
                    cffex_stat["settlement_rows"] += counts.get("settlement_rows", 0)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    cffex_stat["no_data"] += 1
                else:
                    cffex_stat["errors"] += 1
            except Exception:
                cffex_stat["errors"] += 1
            # CFFEX rate-limit: ≥1s between requests.
            time.sleep(max(1.0, pause_seconds))
        # Adjust overall tallies.
        result["downloaded"] += cffex_stat["downloaded"]
        result["no_data"] += cffex_stat["no_data"]
        result["errors"] += cffex_stat["errors"]
        result["market_rows"] += cffex_stat["market_rows"]
    result["per_exchange"]["CFFEX"] = cffex_stat
    return result


def write_dashboard(database: PositionsDatabase, output_path: str | Path, days: int | None = None) -> Path:
    payload = database.dashboard_payload(days=days)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    html, sidecar_json = build_dashboard_html(payload=payload, latest_date=payload.get("latestDate", ""), sidecar=True)
    output.write_text(html, encoding="utf-8")
    if sidecar_json:
        (output.parent / "member_daily.json").write_text(sidecar_json, encoding="utf-8")
    return output


def run_daily(args: argparse.Namespace) -> dict:
    database = PositionsDatabase(args.db)
    status = database.status()
    earliest = date.fromisoformat(status["earliest_date"]) if status["earliest_date"] else DEFAULT_START_DATE
    start = parse_date(args.start_date, earliest)
    end = parse_date(args.end_date, date.today())
    positions_update = update_incremental(database, start, end, timeout=args.timeout)
    market_update = update_market_incremental(database, start, end, timeout=args.timeout)
    dashboard = write_dashboard(database, args.dashboard, days=args.dashboard_days)
    return {
        "positions_update": positions_update,
        "market_update": market_update,
        "status": database.status(),
        "dashboard": str(dashboard.resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="上海期货交易所每日会员持仓数据系统")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化 SQLite 数据库")
    init_parser.add_argument("--db", default=DEFAULT_DB)

    import_parser = subparsers.add_parser("import-csv", help="导入历史 CSV 到 SQLite")
    import_parser.add_argument("paths", nargs="+")
    import_parser.add_argument("--db", default=DEFAULT_DB)

    update_parser = subparsers.add_parser("update", help="只下载数据库中缺失的工作日")
    update_parser.add_argument("--db", default=DEFAULT_DB)
    update_parser.add_argument("--start-date")
    update_parser.add_argument("--end-date")
    update_parser.add_argument("--timeout", type=int, default=30)

    report_parser = subparsers.add_parser(
        "report",
        help="输出某个品种近 N 日前 5 席位持仓汇总报表（可选调用 DeepSeek 生成 AI 纯文本分析）",
    )
    report_parser.add_argument("--db", default=DEFAULT_DB)
    report_parser.add_argument("--product", required=True, help="品种名称，例如 铜")
    report_parser.add_argument("--days", type=int, default=5, choices=(5, 10, 15), help="回滑天数，仅允许 5/10/15")
    report_parser.add_argument("--ai", action="store_true", help="调用 DeepSeek 生成中文分析")
    report_parser.add_argument("--model", default=DEEPSEEK_DEFAULT_MODEL, help=f"DeepSeek 模型，默认 {DEEPSEEK_DEFAULT_MODEL}")
    report_parser.add_argument("--output", help="输出 JSON 文件路径；不填则只打印到 stdout")

    market_update_parser = subparsers.add_parser(
        "market-update",
        help="下载缺失交易日的结算行情并计算合约市场规模",
    )
    market_update_parser.add_argument("--db", default=DEFAULT_DB)
    market_update_parser.add_argument("--start-date")
    market_update_parser.add_argument("--end-date")
    market_update_parser.add_argument("--timeout", type=int, default=30)

    dashboard_parser = subparsers.add_parser("dashboard", help="从 SQLite 聚合并生成网页")
    dashboard_parser.add_argument("--db", default=DEFAULT_DB)
    dashboard_parser.add_argument("--output", default=DEFAULT_DASHBOARD)
    dashboard_parser.add_argument("--days", type=int)

    daily_parser = subparsers.add_parser("daily", help="增量更新数据库并重建网页")
    daily_parser.add_argument("--db", default=DEFAULT_DB)
    daily_parser.add_argument("--start-date")
    daily_parser.add_argument("--end-date")
    daily_parser.add_argument("--timeout", type=int, default=30)
    daily_parser.add_argument("--dashboard", default=DEFAULT_DASHBOARD)
    daily_parser.add_argument("--dashboard-days", type=int)

    status_parser = subparsers.add_parser("status", help="查看数据库状态")
    status_parser.add_argument("--db", default=DEFAULT_DB)

    serve_parser = subparsers.add_parser("serve", help="启动本地网页服务（含 /report AI 报表接口）")
    serve_parser.add_argument("--directory", default="output/shfe_system")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--db", default=DEFAULT_DB)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        database = PositionsDatabase(args.db)
        database.initialize()
        print(json.dumps(database.status(), ensure_ascii=False, indent=2))
    elif args.command == "import-csv":
        database = PositionsDatabase(args.db)
        total = 0
        for path in args.paths:
            imported = database.import_csv(path)
            total += imported
            print(f"导入 {path}: {imported} 行")
        print(f"累计导入: {total} 行")
        print(json.dumps(database.status(), ensure_ascii=False, indent=2))
    elif args.command == "update":
        database = PositionsDatabase(args.db)
        status = database.status()
        earliest = date.fromisoformat(status["earliest_date"]) if status["earliest_date"] else DEFAULT_START_DATE
        start = parse_date(args.start_date, earliest)
        end = parse_date(args.end_date, date.today())
        print(json.dumps(update_incremental(database, start, end, args.timeout), ensure_ascii=False, indent=2))
        print(json.dumps(database.status(), ensure_ascii=False, indent=2))
    elif args.command == "report":
        database = PositionsDatabase(args.db)
        summary = top5_daily_summary(database, args.product, days=args.days, top_n=5)
        result: dict = {"summary": summary, "ai_summary": None}
        if args.ai:
            try:
                prompt = build_ai_prompt(summary)
                api_resp = call_deepseek(prompt, model=args.model)
                result["ai_summary"] = extract_text(api_resp)
            except Exception as exc:
                result["ai_error"] = str(exc)
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(payload, encoding="utf-8")
            print(f"报表已写入: {args.output}")
        else:
            print(payload)
    elif args.command == "dashboard":
        output = write_dashboard(PositionsDatabase(args.db), args.output, args.days)
        print(f"网页: {output.resolve()}")
    elif args.command == "market-update":
        database = PositionsDatabase(args.db)
        status = database.status()
        earliest = date.fromisoformat(status["earliest_date"]) if status["earliest_date"] else DEFAULT_START_DATE
        start = parse_date(args.start_date, earliest)
        end = parse_date(args.end_date, date.today())
        print(
            json.dumps(
                update_market_incremental(database, start, end, args.timeout),
                ensure_ascii=False,
                indent=2,
            )
        )
        print(json.dumps(database.status(), ensure_ascii=False, indent=2))
    elif args.command == "daily":
        print(json.dumps(run_daily(args), ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(PositionsDatabase(args.db).status(), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        directory = Path(args.directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        handler_cls = make_report_handler(str(directory), args.db)
        server = ThreadingHTTPServer((args.host, args.port), handler_cls)
        print(f"网页服务: http://{args.host}:{args.port}/")
        print(f"报表接口: http://{args.host}:{args.port}/report?product=<品种>&days=5&ai=1")
        print(f"数据库: {args.db}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
