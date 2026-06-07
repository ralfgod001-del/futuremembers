"""Top-N seat aggregation reports with optional DeepSeek AI summary.

The :func:`top5_daily_summary` function returns a structured dict describing
the last ``days`` trading days for a given product. Each day records the top
``N`` seats by long and short positions (aggregated across contracts), the
total longs/shorts of those top-N seats, the net long-short position, and the
day-over-day change relative to the previous trading day in the window.

The :func:`call_deepseek` helper posts a chat-completion request to DeepSeek's
HTTP API. The API key is read from the ``DEEPSEEK_API_KEY`` environment
variable unless passed explicitly.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

from .database import PositionsDatabase


def _project_root() -> Path:
    """Return the project root directory.

    Walks up from this file until it finds a parent that contains both
    ``futures_positions`` and ``tests`` (or ``.git``), falling back to the
    current file's parent.
    """
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        if (parent / "futures_positions").is_dir() and (parent / "requirements.txt").exists():
            return parent
    return here.parent


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    The file is parsed as a minimal subset of the dotenv format:
      - one assignment per line, ``KEY=VALUE``
      - ``#`` introduces a comment
      - surrounding quotes (single or double) are stripped
      - blank lines are skipped
    By default existing env vars are preserved (``override=False``); pass
    ``override=True`` to force-overwrite. Only the calling process is affected.
    """
    target = Path(path) if path else _project_root() / ".env"
    if not target.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.isidentifier() and not all(c.isalnum() or c == "_" for c in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


# Auto-load project-local .env on import so CLI/serve/report commands pick it up.
load_env_file()


logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-pro"


def top5_daily_summary(
    database: PositionsDatabase,
    product: str,
    days: int = 5,
    top_n: int = 5,
    metric_date: date | None = None,
) -> dict[str, Any]:
    """Build a top-N seat summary for the last ``days`` trading days.

    Parameters
    ----------
    database:
        Initialized :class:`PositionsDatabase`.
    product:
        Product name (e.g. ``"铜"``).
    days:
        Number of most-recent trading days to include. Must be one of the
        values the dashboard exposes (5/10/15) but no validation is done here.
    top_n:
        Number of seats to track per side per day. Default 5.
    metric_date:
        Optional anchor date; defaults to the latest trade date in the
        ``positions`` table.

    Returns
    -------
    dict with keys ``product``, ``days``, ``top_n``, ``anchor_date``,
    ``trade_dates`` (list of date strings in ascending order), and ``days_data``
    (list of per-day dicts sorted ascending by trade_date).
    """
    database.initialize()
    with database.session() as conn:
        if metric_date is None:
            row = conn.execute(
                "SELECT MAX(trade_date) AS latest FROM positions WHERE product=?",
                (product,),
            ).fetchone()
            metric_date = date.fromisoformat(row["latest"]) if row and row["latest"] else date.today()

        # Fetch the last `days` trade dates that have positions for this product, up to metric_date.
        date_rows = conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM positions
            WHERE product=? AND trade_date<=?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (product, metric_date.isoformat(), days),
        ).fetchall()
        trade_dates = [r["trade_date"] for r in date_rows][::-1]  # ascending

        # One extra day before the window, used to compute day-over-day change for the first day.
        prev_day_row = conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM positions
            WHERE product=? AND trade_date<?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (product, trade_dates[0]) if trade_dates else (product, metric_date.isoformat()),
        ).fetchone()
        prev_day = prev_day_row["trade_date"] if prev_day_row else None

        scope_dates = list(trade_dates)
        if prev_day:
            scope_dates.insert(0, prev_day)

        # Pull aggregate (member, metric, total) per date in one query.
        member_rows = conn.execute(
            f"""
            SELECT trade_date, metric, member, SUM(value) AS total
            FROM positions
            WHERE product=? AND trade_date IN ({",".join("?" for _ in scope_dates)})
            GROUP BY trade_date, metric, member
            """,
            (product, *scope_dates),
        ).fetchall()

        # Fetch OI-weighted settlement price per day for this product.
        price_rows = conn.execute(
            """
            SELECT m.trade_date,
                   SUM(m.open_interest) AS open_interest,
                   CASE WHEN SUM(m.open_interest) > 0
                       THEN SUM(m.open_interest * m.settlement_price) * 1.0 / SUM(m.open_interest)
                       ELSE NULL END AS settlement_price
            FROM contract_daily_market AS m
            JOIN contract_specs AS s
              ON s.exchange = m.exchange
             AND s.product_code = m.product_code
             AND m.trade_date >= s.effective_from
             AND (s.effective_to IS NULL OR m.trade_date <= s.effective_to)
            WHERE (m.product_name = ? OR m.product_code = ?)
              AND m.trade_date IN (""" + ",".join("?" for _ in scope_dates) + """)
            GROUP BY m.trade_date
            ORDER BY m.trade_date
            """,
            (product, product, *scope_dates),
        ).fetchall()
        price_by_date: dict[str, dict[str, Any]] = {
            r["trade_date"]: {
                "settlement_price": r["settlement_price"],
                "open_interest": r["open_interest"],
            }
            for r in price_rows
        }

        # Reshape: by_date[trade_date][metric] = list of (member, total) sorted desc.
        by_date: dict[str, dict[str, list[tuple[str, float]]]] = {}
        for r in member_rows:
            by_date.setdefault(r["trade_date"], {}).setdefault(r["metric"], []).append((r["member"], r["total"]))
        for ddict in by_date.values():
            for metric in ddict:
                ddict[metric].sort(key=lambda x: x[1], reverse=True)

        days_data: list[dict[str, Any]] = []

        # If we fetched one extra day before the window, pre-seed prev_snapshot
        # so the first slot has a meaningful day-over-day change.
        prev_snapshot: dict[str, Any] | None = None
        if prev_day:
            prev = by_date.get(prev_day, {})
            prev_longs = prev.get("long", [])[:top_n]
            prev_shorts = prev.get("short", [])[:top_n]
            prev_long_total = sum(v for _, v in prev_longs)
            prev_short_total = sum(v for _, v in prev_shorts)
            prev_snapshot = {
                "long_total": prev_long_total,
                "short_total": prev_short_total,
                "net": prev_long_total - prev_short_total,
            }

        prev_price: float | None = None
        # If we have a price for prev_day, use it as the baseline.
        if prev_day and prev_day in price_by_date:
            prev_price = price_by_date[prev_day]["settlement_price"]

        # Member-level changes compare each day against the previous trading
        # day in/by before the window. Track it explicitly instead of inferring
        # the index from the growing `days_data` list.
        prev_member_date = prev_day
        for d in trade_dates:
            today = by_date.get(d, {})
            longs = today.get("long", [])[:top_n]
            shorts = today.get("short", [])[:top_n]
            long_total = sum(v for _, v in longs)
            short_total = sum(v for _, v in shorts)
            net = long_total - short_total

            # Day-over-day changes for positions.
            change_long = change_short = change_net = None
            if prev_snapshot is not None:
                change_long = long_total - prev_snapshot["long_total"]
                change_short = short_total - prev_snapshot["short_total"]
                change_net = net - prev_snapshot["net"]

            # Settlement price + price change.
            price_info = price_by_date.get(d, {})
            settlement_price = price_info.get("settlement_price")
            price_change = None
            price_change_pct = None
            if settlement_price is not None and prev_price is not None:
                price_change = settlement_price - prev_price
                price_change_pct = price_change / prev_price * 100 if prev_price else None

            # Member-level day-over-day position changes (vs same members yesterday).
            prev_today = by_date.get(prev_member_date, {}) if prev_member_date else {}
            prev_member_map: dict[str, dict[str, float]] = {}
            for metric_key in ("long", "short"):
                for m, v in prev_today.get(metric_key, []):
                    prev_member_map.setdefault(m, {})[metric_key] = v

            def _member_change(member: str, metric: str, current_val: float) -> float | None:
                pv = prev_member_map.get(member, {}).get(metric)
                if pv is None:
                    return None
                return current_val - pv

            top_long_detail = [
                {"member": m, "value": v, "change": _member_change(m, "long", v)}
                for m, v in longs
            ]
            top_short_detail = [
                {"member": m, "value": v, "change": _member_change(m, "short", v)}
                for m, v in shorts
            ]

            days_data.append({
                "trade_date": d,
                "top_long": top_long_detail,
                "top_short": top_short_detail,
                "long_total": long_total,
                "short_total": short_total,
                "net_long_short": net,
                "change_long": change_long,
                "change_short": change_short,
                "change_net": change_net,
                "settlement_price": settlement_price,
                "price_change": price_change,
                "price_change_pct": price_change_pct,
                "market_open_interest": price_info.get("open_interest"),
            })

            prev_member_date = d
            prev_snapshot = {
                "long_total": long_total,
                "short_total": short_total,
                "net": net,
            }
            if settlement_price is not None:
                prev_price = settlement_price

    return {
        "product": product,
        "days": days,
        "top_n": top_n,
        "anchor_date": metric_date.isoformat(),
        "trade_dates": trade_dates,
        "days_data": days_data,
    }


def build_ai_prompt(summary: dict[str, Any]) -> str:
    """Render the summary dict as a Chinese-facing prompt for DeepSeek.

    The prompt asks the model to perform a dual-axis analysis combining
    settlement-price movements with top-seat position changes, and to
    enumerate possible motivations for each notable member-level move.
    """
    product = summary["product"]
    days = summary["days"]
    top_n = summary["top_n"]

    # Build a compact, human-readable data table for the prompt so the model
    # doesn't have to parse raw JSON arrays. This improves reasoning quality.
    lines_data = [
        f"\u54c1\u79cd\uff1a{product}\uff0c\u7a97\u53e3\uff1a\u8fd1 {days} \u4e2a\u4ea4\u6613\u65e5\uff0cTop{top_n} \u5e2d\u4f4d\u6c47\u603b\u3002",
        "",
        "| \u4ea4\u6613\u65e5 | \u7ed3\u7b97\u4ef7 | \u4ef7\u53d8 | \u591a\u5934\u603b\u8ba1 | \u591a\u5934\u53d8\u5316 | \u7a7a\u5934\u603b\u8ba1 | \u7a7a\u5934\u53d8\u5316 | \u51c0\u591a\u7a7a | \u51c0\u5934\u53d8\u5316 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for d in summary.get("days_data", []):
        sp = d.get("settlement_price")
        pc = d.get("price_change")
        sp_str = f"{sp:,.0f}" if sp is not None else "\u2014"
        pc_str = f"{pc:+,.0f}" if pc is not None else "\u2014"
        lt = d.get("long_total", 0)
        lc = d.get("change_long")
        lc_str = f"{lc:+,.0f}" if lc is not None else "\u2014"
        st = d.get("short_total", 0)
        sc = d.get("change_short")
        sc_str = f"{sc:+,.0f}" if sc is not None else "\u2014"
        net = d.get("net_long_short", 0)
        nc = d.get("change_net")
        nc_str = f"{nc:+,.0f}" if nc is not None else "\u2014"
        lines_data.append(
            f"| {d['trade_date']} | {sp_str} | {pc_str} | {lt:,.0f} | {lc_str} | {st:,.0f} | {sc_str} | {net:,.0f} | {nc_str} |"
        )

    # Member-level detail for the last trading day.
    if summary.get("days_data"):
        last = summary["days_data"][-1]
        lines_data.append("")
        lines_data.append(f"\u6700\u65b0\u65e5\uff08{last['trade_date']}\uff09Top{top_n} \u4f1a\u5458\u660e\u7ec6\uff1a")
        lines_data.append("**\u591a\u5934**\uff1a")
        for r in last.get("top_long", []):
            ch = r.get("change")
            ch_str = f"\uff08\u65e5\u53d8 {ch:+,.0f}\uff09" if ch is not None else ""
            lines_data.append(f"  - {r['member']}\uff1a{r['value']:,.0f} {ch_str}")
        lines_data.append("**\u7a7a\u5934**\uff1a")
        for r in last.get("top_short", []):
            ch = r.get("change")
            ch_str = f"\uff08\u65e5\u53d8 {ch:+,.0f}\uff09" if ch is not None else ""
            lines_data.append(f"  - {r['member']}\uff1a{r['value']:,.0f} {ch_str}")

    lines = [
        f"\u4f60\u662f\u4e13\u4e1a\u671f\u8d27\u5e02\u573a\u5206\u6790\u5e08\u3002\u4ee5\u4e0b\u662f\u4e0a\u671f\u6240 {product} \u8fd1 {days} \u4e2a\u4ea4\u6613\u65e5\u7684\u524d {top_n} \u5e2d\u4f4d\u6301\u4ed3\u4e0e\u54c1\u79cd\u7ed3\u7b97\u4ef7\u6570\u636e\u3002",
        "",
        "\u8bf7\u8f93\u51fa 600\u20131000 \u5b57\u7684\u4e2d\u6587\u5206\u6790\u62a5\u544a\uff0c\u7ed3\u6784\u5982\u4e0b\uff1a",
        "",
        "**\u4e00\u3001\u4ef7\u683c\u8d70\u52bf\u4e0e\u6301\u4ed3\u53d8\u52a8\u7684\u53cc\u91cd\u89c2\u5bdf**",
        "\u7ed3\u5408\u7ed3\u7b97\u4ef7\u7684\u65e5\u5ea6\u6da8\u8dcc\u4e0e\u591a\u7a7a\u603b\u6301\u4ed3\u7684\u65e5\u5ea6\u53d8\u5316\uff0c\u5206\u6790\u4ef7\u683c\u4e0e\u6301\u4ed3\u662f\u540c\u5411\u8fd8\u662f\u80cc\u79bb\u3002\u4f8b\u5982\uff1a\u4ef7\u683c\u4e0a\u6da8\u4f46\u591a\u5934\u51cf\u4ed3\u53ef\u80fd\u610f\u5473\u7740\u591a\u5934\u6b62\u76c8\u51fa\u5c40\uff1b\u4ef7\u683c\u4e0b\u8dcc\u4f46\u7a7a\u5934\u51cf\u4ed3\u53ef\u80fd\u610f\u5473\u7740\u7a7a\u5934\u6b62\u76c8\u56de\u8865\u3002",
        "",
        "**\u4e8c\u3001\u5934\u90e8\u4f1a\u5458\u52a8\u5411\u9010\u4e00a\u5206\u6790**",
        "\u5bf9\u6700\u65b0\u4ea4\u6613\u65e5\u7684\u591a\u5934\u548c\u7a7a\u5934 Top5 \u4f1a\u5458\uff0c\u9010\u4e00a\u5206\u6790\u5176\u52a0\u4ed3/\u51cf\u4ed3\u7684\u53ef\u80fd\u539f\u56e0\u548c\u52a8\u673a\uff0c\u5305\u62ec\u4f46\u4e0d\u9650\u4e8e\uff1a",
        "- \u65b9\u5411\u6027\u8d8b\u52bf\u8ddf\u968f\uff08\u987a\u52bf\u52a0\u4ed3\uff09\u8fd8\u662f\u9006\u52bf\u5e03\u5c40\uff08\u9006\u52bf\u5efa\u4ed3/\u6b62\u76c8\uff09",
        "- \u662f\u5426\u4e3a\u5957\u4fdd\u64cd\u4f5c\uff08\u73b0\u8d27\u5e97\u5957\u4fdd\u3001\u8de8\u671f\u5957\u4fdd\uff09",
        "- \u662f\u5426\u4e3a\u8d44\u91d1\u7ba1\u7406\u8d37\u6b3e\u8c03\u6574\u6216\u65c5\u5ba2\u5927\u6237\u8d44\u91d1\u5165\u573a/\u79bb\u573a",
        "- \u662f\u5426\u4e0e\u57fa\u672c\u9762\u6d88\u606f\u9762\uff08\u4f9b\u5e94\u7aef/\u9700\u6c42\u7aef/\u5e93\u5b58/\u653f\u7b56\uff09\u76f8\u5173",
        "- \u662f\u5426\u5b58\u5728\u591a\u7a7a\u5bf9\u51b3\uff08\u540c\u4e00a\u4f1a\u5458\u540c\u65f6\u5927\u91cf\u591a\u7a7a\u53cc\u4ed3\uff09",
        "\u6bcf\u4e2a\u4f1a\u5458\u7ed9\u51fa 1\u20132 \u53e5\u7b80\u8981\u5224\u65ad\u3002",
        "",
        "**\u4e09\u3001\u5f02\u5e38\u4e0e\u98ce\u9669\u63d0\u793a**",
        "\u6307\u51fa\u6570\u636e\u4e2d\u7684\u5f02\u5e38\u503c\u3001\u5e2d\u4f4d\u9aa4\u53d8\u3001\u4ef7\u4ed3\u80cc\u79bb\u4fe1\u53f7\u7b49\u9700\u8b66\u60d5\u7684\u73b0\u8c61\u3002",
        "",
        "\u8981\u6c42\uff1a\u7ed3\u8bba\u5148\u884c\u3001\u7406\u7531\u7b80\u6d01\u3001\u907f\u514d\u5197\u957f\u590d\u8ff0\u539f\u59cb\u6570\u636e\u3002",
        "",
        "\u6570\u636e\u6982\u89c8\uff1a",
        "",
    ]
    lines.extend(lines_data)
    return "\n".join(lines)


def call_deepseek(
    prompt: str,
    api_key: str | None = None,
    model: str | None = None,
    timeout: int = 120,
    max_tokens: int = 4096,
    http: requests.Session | None = None,
    retries: int = 2,
    backoff: float = 1.5,
) -> dict[str, Any]:
    """Call DeepSeek's chat-completion endpoint.

    The default model is :data:`DEEPSEEK_DEFAULT_MODEL` but can be overridden
    by the ``DEEPSEEK_MODEL`` environment variable or the ``model`` argument.
    ``max_tokens`` defaults to 4096 because reasoning models (deepseek-v4-pro)
    consume tokens for internal chain-of-thought before emitting visible text.
    """
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DeepSeek API key missing. Set DEEPSEEK_API_KEY in .env at the project root "
            "or in the DEEPSEEK_API_KEY environment variable."
        )
    effective_model = model or os.environ.get("DEEPSEEK_MODEL") or DEEPSEEK_DEFAULT_MODEL
    session = http or requests.Session()
    payload: dict[str, Any] = {
        "model": effective_model,
        "messages": [
            {"role": "system", "content": "你是一名期货市场数据分析助手，输出简洁、结论先行。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    # response_format is not supported by reasoning models like deepseek-v4-pro.
    # Only set it for non-reasoning models.
    if "reason" not in effective_model and "v4" not in effective_model:
        payload["response_format"] = {"type": "text"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # Transient errors (HTTP 429/5xx, connection drops) are retried with
    # exponential backoff so a single blip does not fail an entire report.
    last_exc: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            response = session.post(DEEPSEEK_API_URL, headers=headers, data=body, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            transient = status is not None and (status == 429 or status >= 500)
            if attempt < retries and transient:
                wait = backoff * (2 ** attempt)
                logger.warning(
                    "DeepSeek returned HTTP %s (attempt %d/%d); retrying in %.1fs",
                    status, attempt + 1, retries + 1, wait,
                )
                last_exc = exc
                time.sleep(wait)
                continue
            raise
        except requests.ConnectionError as exc:
            if attempt < retries:
                wait = backoff * (2 ** attempt)
                logger.warning(
                    "DeepSeek connection error (attempt %d/%d); retrying in %.1fs: %s",
                    attempt + 1, retries + 1, wait, exc,
                )
                last_exc = exc
                time.sleep(wait)
                continue
            raise
    # Defensive: all retries exhausted without returning or re-raising.
    raise last_exc if last_exc else RuntimeError("DeepSeek call failed after retries")


def extract_text(api_response: dict[str, Any]) -> str:
    """Pull the assistant text out of a DeepSeek chat completion response.

    Returns an empty string when the expected `choices[0].message.content`
    path is absent, but logs a short preview so a silently empty AI section
    is still diagnosable.
    """
    try:
        return api_response["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        logger.warning("DeepSeek response missing expected content path: %r", repr(api_response)[:300])
        return ""




def build_report_page(summary: dict[str, Any], ai_summary: str | None = None, ai_error: str | None = None) -> str:
    """Render a standalone HTML report page from a top5 summary dict.

    The page is self-contained (no external JS/CSS) and includes:
      - A toolbar with Save-as-HTML, Save-as-JSON, and Print buttons.
      - A summary table (top-5 long/short per day with day-over-day deltas).
      - The AI analysis text (or an error/info block).
    """
    import html as _html
    from urllib.parse import quote as _urlquote

    product = summary.get("product", "")
    days = summary.get("days", 0)
    anchor = summary.get("anchor_date", "")
    trade_dates = summary.get("trade_dates", [])
    days_data = summary.get("days_data", [])
    ts = _datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def fmt_int(v):
        try:
            return f"{int(v):,}"
        except (TypeError, ValueError):
            return str(v or 0)

    def fmt_delta(v):
        if v is None:
            return "—"
        try:
            n = int(v)
            return f"{'+' if n > 0 else ''}{n:,}"
        except (TypeError, ValueError):
            return str(v)

    def fmt_price(v):
        if v is None:
            return "—"
        try:
            return f"{v:,.0f}"
        except (TypeError, ValueError):
            return str(v)

    def fmt_pct(v):
        if v is None:
            return "—"
        try:
            return f"{'+' if v >= 0 else ''}{v:.2f}%"
        except (TypeError, ValueError):
            return str(v)

    table_rows = []
    for d in days_data:
        # Build member detail strings with per-member change indicators.
        def _member_str(r):
            m = _html.escape(r['member'])
            v = fmt_int(r['value'])
            ch = r.get('change')
            if ch is None:
                return f"{m}({v})"
            arrow = "↑" if ch > 0 else ("↓" if ch < 0 else "–")
            return f"{m}({v}{arrow}{fmt_int(abs(ch))})"
        top_long = "; ".join(_member_str(r) for r in d.get("top_long", []))
        top_short = "; ".join(_member_str(r) for r in d.get("top_short", []))

        # Color the price change cell.
        pc = d.get("price_change")
        price_cls = "pos" if (pc is not None and pc > 0) else ("neg" if (pc is not None and pc < 0) else "")
        price_change_str = fmt_price(pc)
        if pc is not None:
            price_change_str = ("+" if pc > 0 else "") + price_change_str

        table_rows.append(
            f"<tr>"
            f"<td>{_html.escape(d['trade_date'])}</td>"
            f"<td>{fmt_price(d.get('settlement_price'))}</td>"
            f"<td class=\"{price_cls}\">{price_change_str}</td>"
            f"<td>{fmt_pct(d.get('price_change_pct'))}</td>"
            f"<td>{fmt_int(d['long_total'])}</td>"
            f"<td>{fmt_delta(d['change_long'])}</td>"
            f"<td>{fmt_int(d['short_total'])}</td>"
            f"<td>{fmt_delta(d['change_short'])}</td>"
            f"<td>{fmt_int(d['net_long_short'])}</td>"
            f"<td>{fmt_delta(d['change_net'])}</td>"
            f"<td class=\"top-list\">{top_long}</td>"
            f"<td class=\"top-list\">{top_short}</td>"
            f"</tr>"
        )
    table_body = "\n".join(table_rows)

    if ai_summary:
        # AI output uses markdown (**bold**, - bullets, ### headings). Render
        # it client-side via a small inline markdown parser so the user sees
        # a styled report instead of raw markdown syntax.
        import base64 as _b64
        ai_b64 = _b64.b64encode(ai_summary.encode("utf-8")).decode("ascii")
        ai_block = (
            '<div class="ai-box" id="ai-content" data-ai-b64="'
            + ai_b64
            + '"></div>'
        )
    elif ai_error:
        ai_block = f'<div class="ai-box ai-error">DeepSeek 调用失败：{_html.escape(ai_error)}</div>'
    else:
        ai_block = '<div class="ai-box ai-error">未设置 DEEPSEEK_API_KEY，已跳过 AI 调用。</div>'

    # Embed raw JSON for save-as-JSON inside a <script type="application/json">
    # block. This avoids base64 encoding overhead (which can balloon to several
    # hundred KB for multi-day reports) and lets the browser parse it natively.
    import json as _json_mod
    raw_payload = _json_mod.dumps(
        {"summary": summary, "ai_summary": ai_summary, "ai_error": ai_error},
        ensure_ascii=False, indent=2,
    )
    # Neutralise the </script> break-out vector (see comment above): member
    # names embedded in this JSON could otherwise terminate the
    # <script type="application/json"> block.
    raw_payload = (
        raw_payload.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )

    safe_product = _html.escape(product)
    safe_product_url = _urlquote(product, safe="")
    safe_ts = _html.escape(ts)
    safe_anchor = _html.escape(anchor)
    date_tag = _datetime.now().strftime("%Y%m%d")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 报表：{safe_product} 近 {days} 日</title>
<style>
  body {{ font-family: "Microsoft YaHei","Segoe UI",Arial,sans-serif; background:#f6f7f9; color:#172033; margin:0; padding:24px; }}
  .header {{ max-width:1100px; margin:0 auto 20px; }}
  .header h1 {{ margin:0 0 6px; font-size:24px; }}
  .header .meta {{ color:#6b7280; font-size:13px; margin-bottom:16px; }}
  .toolbar {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:20px; }}
  .toolbar button {{ padding:9px 18px; border:1px solid #2563eb; background:#2563eb; color:#fff; border-radius:6px; font-size:14px; cursor:pointer; font-weight:600; }}
  .toolbar button:hover {{ background:#1d4ed8; }}
  .toolbar button.secondary {{ background:#fff; color:#2563eb; }}
  .toolbar button.secondary:hover {{ background:#eff6ff; }}
  .section {{ max-width:1100px; margin:0 auto 20px; background:#fff; border:1px solid #dfe5ee; border-radius:8px; padding:18px 22px; overflow-x:auto; }}
  .section h2 {{ margin:0 0 10px; font-size:16px; color:#374151; }}
  .table-wrap {{ overflow-x:auto; -webkit-overflow-scrolling:touch; margin:0 -22px; padding:0 22px; }}
  table {{ min-width:100%; border-collapse:collapse; font-size:12px; }}
  th,td {{ padding:7px 9px; border-bottom:1px solid #e5e7eb; text-align:right; white-space:nowrap; }}
  th:first-child,td:first-child {{ text-align:left; }}
  th {{ color:#6b7280; font-weight:600; background:#fafafa; white-space:nowrap; }}
  td.top-list {{ text-align:left; white-space:normal; word-break:keep-all; max-width:220px; line-height:1.6; }}
  td.pos {{ color:#dc2626; font-weight:600; }}
  td.neg {{ color:#059669; font-weight:600; }}
  th.col-top {{ white-space:nowrap; }}
  .ai-box {{ background:#eff6ff; border:1px solid #bfdbfe; border-radius:8px; padding:16px 22px; font-size:14px; line-height:1.8; color:#1e3a8a; }}
  .ai-box h2 {{ font-size:17px; margin:14px 0 8px; color:#1e3a8a; border-bottom:1px solid #bfdbfe; padding-bottom:4px; }}
  .ai-box h3 {{ font-size:15px; margin:12px 0 6px; color:#1e3a8a; }}
  .ai-box h4 {{ font-size:14px; margin:10px 0 4px; color:#1e3a8a; }}
  .ai-box p {{ margin:6px 0; }}
  .ai-box ul, .ai-box ol {{ margin:6px 0 6px 18px; padding:0; }}
  .ai-box li {{ margin:3px 0; }}
  .ai-box strong {{ color:#0c4a6e; font-weight:700; }}
  .ai-box em {{ font-style:italic; }}
  .ai-box code {{ background:#dbeafe; padding:1px 4px; border-radius:3px; font-family:Consolas,monospace; font-size:13px; }}
  .ai-box blockquote {{ border-left:3px solid #93c5fd; margin:8px 0; padding:4px 12px; color:#3730a3; font-style:italic; background:rgba(255,255,255,0.4); }}
  .ai-box.ai-error {{ background:#fef2f2; border-color:#fecaca; color:#991b1b; }}
  .toolbar .tb-btn {{ padding:9px 18px; border:1px solid #2563eb; background:#2563eb; color:#fff !important; border-radius:6px; font-size:14px; cursor:pointer; font-weight:600; text-decoration:none; display:inline-block; }}
  .toolbar .tb-btn:hover {{ background:#1d4ed8; }}
  .toolbar .tb-btn.secondary {{ background:#fff; color:#2563eb !important; }}
  .toolbar .tb-btn.secondary:hover {{ background:#eff6ff; }}
  .toolbar .tb-btn.loading, .toolbar .tb-btn[disabled] {{ opacity:0.55; pointer-events:none; cursor:wait; }}
  @media print {{ .toolbar {{ display:none; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>AI 报表：{safe_product} 近 {days} 日</h1>
  <div class="meta">生成时间：{safe_ts} ｜ 汇总窗口：{len(days_data)} 个交易日 ｜ 锚定日：{safe_anchor}</div>
  <div class="toolbar">
    <a id="btn-save-html" class="tb-btn" href="/report/download?product={safe_product_url}&days={days}&format=html" download="{safe_product}_{days}d_{date_tag}.html"
       onclick="return _onSaveClick('html', this);">保存为 HTML</a>
    <a id="btn-save-json" class="tb-btn secondary" href="/report/download?product={safe_product_url}&days={days}&format=json" download="{safe_product}_{days}d_{date_tag}.json"
       onclick="return _onSaveClick('json', this);">保存为 JSON</a>
    <button class="secondary" onclick="window.print()">打印 / 导出 PDF</button>
    <span id="save-status" style="margin-left:12px;font-size:13px;color:#6b7280;"></span>
  </div>
</div>
<div class="section">
  <h2>结算价×持仓双重视角（前 5 席位汇总\uff0c\u2191=\u52a0\u4ed3 \u2193=\u51cf\u4ed3\uff09</h2>
  <div class="table-wrap">
  <table>
    <thead><tr>
      <th>交易日</th>
      <th>结算价</th><th>价变</th><th>涨跌%</th>
      <th>多头总计</th><th>多头变化</th>
      <th>空头总计</th><th>空头变化</th>
      <th>净多空</th><th>净头变化</th>
      <th class="col-top">多头 Top5</th><th class="col-top">空头 Top5</th>
    </tr></thead>
    <tbody>
{table_body}
    </tbody>
  </table>
  </div>
</div>
<div class="section">
  <h2>DeepSeek 分析</h2>
  {ai_block}
</div>
<script type="application/json" id="report-json">
{raw_payload}
</script>
<script>
  // === Lightweight markdown-to-HTML renderer ===
  // Supports: # ## ### headings, **bold**, *italic*, `code`, - bullet lists,
  // numbered lists, \\n line breaks, > blockquote. Designed to be small and
  // dependency-free for the report popup window.
  function renderMarkdown(md) {{
    if (!md) return "";
    var lines = md.split("\\n");
    var html = [];
    var inList = false;
    var inOl = false;
    for (var i = 0; i < lines.length; i++) {{
      var line = lines[i];
      var trimmed = line.trim();
      // Close any open list when we hit a non-list line
      if (!trimmed.match(/^[-*]\s/) && !trimmed.match(/^\d+[.)]\s/)) {{
        if (inList) {{ html.push("</ul>"); inList = false; }}
        if (inOl) {{ html.push("</ol>"); inOl = false; }}
      }}
      if (trimmed === "") {{
        html.push("");
      }} else if (trimmed.match(/^###\s/)) {{
        html.push("<h4>" + inline(trimmed.replace(/^###\s*/, "")) + "</h4>");
      }} else if (trimmed.match(/^##\s/)) {{
        html.push("<h3>" + inline(trimmed.replace(/^##\s*/, "")) + "</h3>");
      }} else if (trimmed.match(/^#\s/)) {{
        html.push("<h2>" + inline(trimmed.replace(/^#\s*/, "")) + "</h2>");
      }} else if (trimmed.match(/^>\s?/)) {{
        html.push("<blockquote>" + inline(trimmed.replace(/^>\s?/, "")) + "</blockquote>");
      }} else if (trimmed.match(/^[-*]\s/)) {{
        if (!inList) {{ html.push("<ul>"); inList = true; }}
        html.push("<li>" + inline(trimmed.replace(/^[-*]\s+/, "")) + "</li>");
      }} else if (trimmed.match(/^\d+[.)]\s/)) {{
        if (!inOl) {{ html.push("<ol>"); inOl = true; }}
        html.push("<li>" + inline(trimmed.replace(/^\d+[.)]\s+/, "")) + "</li>");
      }} else {{
        html.push("<p>" + inline(trimmed) + "</p>");
      }}
    }}
    if (inList) html.push("</ul>");
    if (inOl) html.push("</ol>");
    return html.join("\\n");
  }}
  function inline(text) {{
    // Escape HTML first to prevent XSS
    text = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    // Bold **text**
    text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    // Italic *text* (but not ** which was already handled)
    text = text.replace(/(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)/g, "<em>$1</em>");
    // Inline code `text`
    text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
    return text;
  }}

  // === Render AI content on load ===
  (function() {{
    var aiBox = document.getElementById("ai-content");
    if (aiBox && aiBox.getAttribute("data-ai-b64")) {{
      try {{
        var b64 = aiBox.getAttribute("data-ai-b64");
        var md = decodeURIComponent(escape(atob(b64)));
        aiBox.innerHTML = renderMarkdown(md);
      }} catch (e) {{
        aiBox.textContent = "Markdown 渲染失败: " + e.message;
      }}
    }}
  }})();

  // === Save buttons (use server-side /report/download endpoint) ===
  // The server endpoint regenerates the report with Content-Disposition: attachment,
  // which is the most reliable way to trigger downloads across all browsers and
  // popup-window contexts. We use fetch() so we can show a loading indicator
  // during the ~30-60s DeepSeek call.
  function _showSaveStatus(msg, isError) {{
    var el = document.getElementById("save-status");
    if (el) {{
      el.textContent = msg;
      el.style.color = isError ? "#dc2626" : "#6b7280";
    }}
  }}
  function _setButtonsDisabled(disabled) {{
    var b1 = document.getElementById("btn-save-html");
    var b2 = document.getElementById("btn-save-json");
    // Use classList so anchors are visually+functionally disabled (the
    // .disabled property on <a> is non-standard and doesn't gate clicks).
    if (b1) {{ if (disabled) b1.classList.add("loading"); else b1.classList.remove("loading"); }}
    if (b2) {{ if (disabled) b2.classList.add("loading"); else b2.classList.remove("loading"); }}
  }}
  // Save buttons are now real <a download> links. The browser handles the
  // download natively via the server's Content-Disposition: attachment
  // response. We only need JS to update the UI and provide a fallback in
  // case the popup-blocker or some browser refuses the download attribute.
  function _onSaveClick(kind, linkElem) {{
    if (linkElem && linkElem.classList && linkElem.classList.contains("loading")) {{
      return false;  // already in flight, ignore re-click
    }}
    _setButtonsDisabled(true);
    _showSaveStatus("正在生成 " + kind.toUpperCase() + " 报表（约 30-60 秒，请等待浏览器开始下载）...", false);
    // Browsers that honor the download attribute + Content-Disposition will
    // trigger the download and keep this page open. Re-enable the buttons
    // after a generous timeout (the actual file save happens async).
    setTimeout(function() {{
      _showSaveStatus("下载已触发。如未弹出，请检查浏览器下载列表或右键链接另存为。", false);
      _setButtonsDisabled(false);
    }}, 5000);
    return true;  // allow the native link navigation to proceed
  }}
  function _downloadBlob(blob, filename) {{
    // Retained for backwards-compatibility / programmatic callers; uses the
    // same approach as before but wrapped in try/catch so it never throws
    // silently. If it fails, the user still has the visible <a download>
    // links in the toolbar.
    try {{
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      setTimeout(function() {{
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }}, 100);
    }} catch (e) {{
      _showSaveStatus("下载失败，请右键点击保存链接： " + e.message, true);
    }}
  }}
  // Legacy stubs (some external code or console users may still call them).
  function saveAsHTML() {{
    var el = document.getElementById("btn-save-html");
    if (el && el.click) {{ el.click(); return; }}
    window.location.href = "/report/download?product=" + encodeURIComponent("{safe_product}") + "&days={days}&format=html";
  }}
  function saveAsJSON() {{
    var el = document.getElementById("btn-save-json");
    if (el && el.click) {{ el.click(); return; }}
    window.location.href = "/report/download?product=" + encodeURIComponent("{safe_product}") + "&days={days}&format=json";
  }}
</script>
</body>
</html>"""


# Need datetime import at module level
from datetime import datetime as _datetime


__all__ = [
    "DEEPSEEK_API_URL",
    "DEEPSEEK_DEFAULT_MODEL",
    "load_env_file",
    "top5_daily_summary",
    "build_ai_prompt",
    "call_deepseek",
    "extract_text",
    "build_report_page",
]
