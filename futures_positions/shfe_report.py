from __future__ import annotations

import argparse
import html
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .adapters import SHFEAdapter
from .models import ExchangeData
from .utils import session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拉取上海期货交易所最近 N 天日交易排名并生成图表")
    parser.add_argument("--days", type=int, default=30, help="最近自然日天数，默认 30")
    parser.add_argument("--end-date", help="结束日期，格式 YYYY-MM-DD；默认今天")
    parser.add_argument("--output-dir", default="output/shfe_30d", help="输出目录")
    parser.add_argument("--timeout", type=int, default=30, help="单个请求超时时间，秒")
    return parser.parse_args()


def date_window(days: int, end_date: date | None = None) -> list[date]:
    end = end_date or date.today()
    start = end - timedelta(days=days - 1)
    return [start + timedelta(days=i) for i in range(days)]


def collect_shfe_range(days: int, end_date: date | None, timeout: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    adapter = SHFEAdapter()
    s = session(timeout)
    frames: list[pd.DataFrame] = []
    log_rows: list[dict[str, str]] = []

    for trade_date in date_window(days, end_date):
        try:
            data: ExchangeData = adapter.fetch(trade_date, s)
            if data.normalized.empty:
                log_rows.append({"date": trade_date.isoformat(), "status": "empty", "message": "无排名数据"})
                continue
            frames.append(data.normalized)
            log_rows.append(
                {
                    "date": trade_date.isoformat(),
                    "status": "ok",
                    "message": f"{len(data.normalized)} rows",
                }
            )
        except Exception as exc:
            log_rows.append({"date": trade_date.isoformat(), "status": "skip", "message": str(exc)})

    if frames:
        normalized = pd.concat(frames, ignore_index=True)
        normalized = normalized.sort_values(["trade_date", "product", "contract", "rank", "metric"])
    else:
        normalized = pd.DataFrame(
            columns=[
                "trade_date",
                "exchange",
                "product",
                "contract",
                "rank",
                "metric",
                "member",
                "value",
                "change",
                "source_url",
                "fetched_at",
            ]
        )
    return normalized, pd.DataFrame(log_rows)


def build_summary(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if df.empty:
        empty = pd.DataFrame()
        return {
            "daily_totals": empty,
            "latest_member_rank": empty,
            "latest_product_totals": empty,
            "latest_contract_totals": empty,
            "member_product_daily": empty,
            "latest_member_product_totals": empty,
        }

    working = df.copy()
    working["value"] = pd.to_numeric(working["value"], errors="coerce").fillna(0)
    working["change"] = pd.to_numeric(working["change"], errors="coerce").fillna(0)
    latest_date = working["trade_date"].max()
    latest = working[working["trade_date"] == latest_date]

    daily_totals = (
        working.groupby(["trade_date", "metric"], as_index=False)["value"]
        .sum()
        .pivot(index="trade_date", columns="metric", values="value")
        .reset_index()
        .fillna(0)
    )
    for col in ["volume", "long", "short"]:
        if col not in daily_totals.columns:
            daily_totals[col] = 0
    daily_totals["net_long_short"] = daily_totals["long"] - daily_totals["short"]

    latest_member_rank = (
        latest.groupby(["metric", "member"], as_index=False)["value"]
        .sum()
        .sort_values(["metric", "value"], ascending=[True, False])
    )
    latest_member_rank["rank"] = latest_member_rank.groupby("metric")["value"].rank(
        method="first", ascending=False
    )
    latest_member_rank = latest_member_rank[latest_member_rank["rank"] <= 30]

    latest_product_totals = (
        latest.groupby(["product", "metric"], as_index=False)["value"]
        .sum()
        .pivot(index="product", columns="metric", values="value")
        .reset_index()
        .fillna(0)
    )
    for col in ["volume", "long", "short"]:
        if col not in latest_product_totals.columns:
            latest_product_totals[col] = 0
    latest_product_totals["open_interest_total"] = latest_product_totals["long"] + latest_product_totals["short"]
    latest_product_totals = latest_product_totals.sort_values("open_interest_total", ascending=False)

    latest_contract_totals = (
        latest.groupby(["product", "contract", "metric"], as_index=False)["value"]
        .sum()
        .pivot(index=["product", "contract"], columns="metric", values="value")
        .reset_index()
        .fillna(0)
    )
    for col in ["volume", "long", "short"]:
        if col not in latest_contract_totals.columns:
            latest_contract_totals[col] = 0
    latest_contract_totals["open_interest_total"] = latest_contract_totals["long"] + latest_contract_totals["short"]
    latest_contract_totals = latest_contract_totals.sort_values("open_interest_total", ascending=False)

    member_product_daily = (
        working.groupby(["trade_date", "member", "product", "metric"], as_index=False)["value"]
        .sum()
        .pivot(index=["trade_date", "member", "product"], columns="metric", values="value")
        .reset_index()
        .fillna(0)
    )
    for col in ["volume", "long", "short"]:
        if col not in member_product_daily.columns:
            member_product_daily[col] = 0
    member_product_daily["net"] = member_product_daily["long"] - member_product_daily["short"]

    latest_member_product_totals = member_product_daily[
        member_product_daily["trade_date"] == latest_date
    ].copy()

    return {
        "daily_totals": daily_totals,
        "latest_member_rank": latest_member_rank,
        "latest_product_totals": latest_product_totals,
        "latest_contract_totals": latest_contract_totals,
        "member_product_daily": member_product_daily,
        "latest_member_product_totals": latest_member_product_totals,
    }


def export_workbook(
    out_dir: Path,
    normalized: pd.DataFrame,
    log: pd.DataFrame,
    summaries: dict[str, pd.DataFrame],
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if normalized.empty:
        tag = datetime.now().strftime("%Y%m%d")
    else:
        tag = f"{normalized['trade_date'].min().replace('-', '')}_{normalized['trade_date'].max().replace('-', '')}"

    csv_path = out_dir / f"shfe_member_positions_{tag}.csv"
    excel_path = out_dir / f"shfe_member_positions_{tag}.xlsx"
    html_path = out_dir / f"shfe_dashboard_{tag}.html"

    normalized.to_csv(csv_path, index=False, encoding="utf-8-sig")
    excel_limit = 1_000_000
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        if len(normalized) <= excel_limit:
            normalized.to_excel(writer, index=False, sheet_name="明细")
        else:
            for idx, start in enumerate(range(0, len(normalized), excel_limit), start=1):
                normalized.iloc[start : start + excel_limit].to_excel(
                    writer,
                    index=False,
                    sheet_name=f"明细{idx}",
                )
        summaries["daily_totals"].to_excel(writer, index=False, sheet_name="日度汇总")
        summaries["latest_member_rank"].to_excel(writer, index=False, sheet_name="最新会员排行")
        summaries["latest_product_totals"].to_excel(writer, index=False, sheet_name="最新品种汇总")
        summaries["latest_contract_totals"].head(100).to_excel(writer, index=False, sheet_name="最新合约Top100")
        summaries["latest_member_product_totals"].to_excel(writer, index=False, sheet_name="最新会员品种")
        log.to_excel(writer, index=False, sheet_name="采集日志")

    out_html, sidecar_json = build_dashboard_html(normalized, summaries, sidecar=True)
    html_path.write_text(out_html, encoding="utf-8")
    if sidecar_json:
        (html_path.parent / "member_daily.json").write_text(sidecar_json, encoding="utf-8")
    return {"csv": csv_path, "excel": excel_path, "html": html_path}


def records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", force_ascii=False))


def build_dashboard_html(
    df: pd.DataFrame | None = None,
    summaries: dict[str, pd.DataFrame] | None = None,
    *,
    payload: dict | None = None,
    latest_date: str | None = None,
    sidecar: bool = False,
) -> tuple[str, str | None]:
    if payload is not None:
        latest_date = latest_date or payload.get("latestDate", "")
    elif df is None or df.empty:
        payload = {
            "rowCount": 0,
            "daily": [],
            "members": [],
            "products": [],
            "contracts": [],
            "memberDaily": [],
            "latestMemberProduct": [],
            "marketDaily": [],
            "marketProducts": [],
            "marketContracts": [],
            "marketLatestDate": "",
        }
        latest_date = ""
    else:
        assert summaries is not None
        latest_date = df["trade_date"].max()
        payload = {
            "rowCount": int(len(df)),
            "daily": records(summaries["daily_totals"]),
            "members": records(summaries["latest_member_rank"]),
            "products": records(summaries["latest_product_totals"]),
            "contracts": records(summaries["latest_contract_totals"].head(30)),
            "memberDaily": records(summaries["member_product_daily"]),
            "latestMemberProduct": records(summaries["latest_member_product_totals"]),
            "marketDaily": [],
            "marketProducts": [],
            "marketContracts": [],
            "marketLatestDate": "",
        }

    member_daily = payload.get("memberDaily", [])
    sidecar_json: str | None
    if sidecar and member_daily:
        sidecar_json = json.dumps(member_daily, ensure_ascii=False, separators=(",", ":"))
        payload["memberDaily"] = []
    else:
        sidecar_json = None
    data_json = json.dumps(payload, ensure_ascii=False)
    sidecar_flag = "true" if sidecar_json else "false"
    title = f"上海期货交易所持仓与市场规模看板 {html.escape(latest_date)}"
    out_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #6b7280;
      --line: #dfe5ee;
      --blue: #2563eb;
      --green: #059669;
      --red: #dc2626;
      --amber: #d97706;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
    }}
    header {{
      padding: 24px 32px 18px;
      background: #111827;
      color: #fff;
    }}
    h1 {{ margin: 0 0 8px; font-size: 26px; font-weight: 700; }}
    .sub {{ color: #cbd5e1; font-size: 13px; }}
    main {{ padding: 20px 32px 32px; }}
    .kpis {{ display: grid; grid-template-columns: repeat(6, minmax(140px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-height: 86px;
    }}
    .label {{ color: var(--muted); font-size: 12px; }}
    .value {{ margin-top: 8px; font-size: 24px; font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: 1.25fr .95fr; gap: 16px; }}
    .wide {{ grid-column: 1 / -1; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-height: 340px;
    }}
    .panel h2 {{ margin: 0 0 12px; font-size: 16px; }}
    .controls {{ display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }}
    select {{
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 7px 10px;
    }}
    svg {{ width: 100%; height: 280px; display: block; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ padding: 8px 9px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    th {{ color: var(--muted); font-weight: 600; background: #fafafa; }}
    .note {{ margin-top: 10px; color: var(--muted); font-size: 12px; }}
    @media (max-width: 1100px) {{
      .kpis {{ grid-template-columns: repeat(3, 1fr); }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 720px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      .kpis {{ grid-template-columns: repeat(2, 1fr); }}
      .value {{ font-size: 20px; }}
    }}
    .header-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .header-bar .actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .header-bar select, .header-bar button {{
      background: #1f2937; color: #f9fafb; border: 1px solid #374151;
      padding: 8px 12px; border-radius: 6px; font-size: 13px;
    }}
    .header-bar button.primary {{
      background: #2563eb; border-color: #3b82f6; font-weight: 600; cursor: pointer;
    }}
    .header-bar button.primary:hover {{ background: #1d4ed8; }}
    .header-bar button.primary:disabled {{ background: #6b7280; border-color: #6b7280; cursor: wait; }}
    .modal-backdrop {{
      display: none; position: fixed; inset: 0; background: rgba(15,23,42,0.55);
      z-index: 50; padding: 24px; overflow-y: auto;
    }}
    .modal-backdrop.open {{ display: flex; align-items: flex-start; justify-content: center; }}
    .modal {{
      background: var(--panel); border-radius: 10px; padding: 22px 24px;
      max-width: 980px; width: 100%; box-shadow: 0 30px 80px rgba(15,23,42,0.4);
      border: 1px solid var(--line);
    }}
    .modal-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }}
    .modal-header h2 {{ margin: 0; font-size: 18px; }}
    .modal-header button {{
      background: transparent; border: none; color: var(--muted);
      font-size: 22px; cursor: pointer; line-height: 1;
    }}
    .modal-section {{ margin-top: 12px; }}
    .modal-section h3 {{ margin: 0 0 8px; font-size: 14px; color: var(--muted); font-weight: 600; }}
    .modal-section table {{ background: #fafafa; }}
    .modal-section .ai-box {{
      background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px;
      padding: 14px 16px; white-space: pre-wrap; font-size: 14px; line-height: 1.7;
      color: #1e3a8a;
    }}
    .modal-section .ai-error {{
      background: #fef2f2; border-color: #fecaca; color: #991b1b;
    }}
    .report-status {{ color: var(--muted); font-size: 13px; margin: 6px 0; }}
    .combobox {{ position: relative; min-width: 220px; }}
    .combobox input[type=text] {{
      width: 100%; box-sizing: border-box;
      background: #1f2937; color: #f9fafb; border: 1px solid #374151;
      padding: 8px 12px; border-radius: 6px; font-size: 13px;
    }}
    .combobox input[type=text]::placeholder {{ color: #94a3b8; }}
    .combobox input[type=text]:focus {{
      outline: none; border-color: #3b82f6; box-shadow: 0 0 0 2px rgba(59,130,246,0.25);
    }}
    .combobox-dropdown {{
      display: none; position: absolute; top: 100%; left: 0; right: 0;
      max-height: 260px; overflow-y: auto;
      background: #ffffff; color: #172033; border: 1px solid #dfe5ee;
      border-radius: 6px; box-shadow: 0 12px 32px rgba(15,23,42,0.18);
      z-index: 40; margin-top: 4px; padding: 4px 0;
    }}
    .combobox-dropdown.open {{ display: block; }}
    .combobox-option {{
      padding: 8px 14px; cursor: pointer; font-size: 13px;
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
    }}
    .combobox-option.active {{ background: #eff6ff; }}
    .combobox-option:hover {{ background: #f1f5f9; }}
    .combobox-option.no-match {{ color: #94a3b8; cursor: default; }}
    .combombx-option-mark {{ font-size: 11px; color: #94a3b8; }}
  </style>
</head>
<body>
<header>
  <div class="header-bar">
    <h1>上海期货交易所持仓与市场规模看板</h1>
    <div class="actions">
      <label style="color:#cbd5e1;font-size:13px">报表品种</label>
      <div class="combobox" id="reportProductCombobox">
        <input type="text" id="reportProductInput" autocomplete="off" placeholder="输入或选择品种" />
        <input type="hidden" id="reportProductSelect" />
        <div class="combobox-dropdown" id="reportProductDropdown"></div>
      </div>
      <label style="color:#cbd5e1;font-size:13px">回滑天数</label>
      <select id="reportDaysSelect">
        <option value="5">5 日</option>
        <option value="10">10 日</option>
        <option value="15">15 日</option>
      </select>
      <button class="primary" id="reportBtn" type="button">生成 AI 报表</button>
    </div>
  </div>
</header>
<main>
  <section class="kpis" id="kpis"></section>
  <section class="grid">
    <div class="panel wide">
      <div class="controls">
        <h2 style="margin-right:auto">交易所合约市场规模趋势</h2>
        <select id="marketTrendMetric">
          <option value="notional_value">名义持仓市值</option>
          <option value="estimated_spec_margin">投机保证金估算（双边）</option>
          <option value="estimated_hedge_margin">套保保证金估算（双边）</option>
          <option value="open_interest">交易所持仓量</option>
        </select>
      </div>
      <svg id="marketTrendChart"></svg>
      <div class="note">名义持仓市值 = 交易所持仓量 × 当日结算价 × 合约乘数。保证金估算按当日交易所多空保证金率计算，不含期货公司加收与组合优惠。</div>
    </div>
    <div class="panel">
      <h2>最新交易日品种名义市值</h2>
      <svg id="marketProductChart"></svg>
    </div>
    <div class="panel">
      <h2>最新交易日品种投机保证金估算</h2>
      <svg id="marketMarginChart"></svg>
    </div>
    <div class="panel wide">
      <h2>最新交易日合约市场规模 Top 30</h2>
      <div id="marketContractTable"></div>
      <div class="note">交易所持仓量采用全市场口径；名义市值为单边合约价值，投机保证金估算为多空双边估算值。</div>
    </div>
    <div class="panel wide">
      <div class="controls">
        <h2 style="margin-right:auto">排名数据趋势</h2>
        <select id="trendMetric">
          <option value="volume">成交量</option>
          <option value="long">多头持仓</option>
          <option value="short">空头持仓</option>
          <option value="net_long_short">多空净额</option>
        </select>
      </div>
      <svg id="trendChart"></svg>
    </div>
    <div class="panel wide">
      <div class="controls">
        <h2 style="margin-right:auto">会员日持仓变化</h2>
        <select id="memberSelect"></select>
        <select id="memberProductSelect"></select>
        <select id="memberMetric">
          <option value="long">多头持仓</option>
          <option value="short">空头持仓</option>
          <option value="net">多空净持仓</option>
        </select>
      </div>
      <svg id="memberChart"></svg>
      <div class="note">会员在某个交易日未进入对应排名时，该日数值按 0 绘制。</div>
    </div>
    <div class="panel wide">
      <div class="controls">
        <h2 style="margin-right:auto">会员持仓 vs 品种结算价</h2>
        <label>会员</label>
        <select id="mp2MemberSelect"></select>
        <label>品种</label>
        <select id="mp2ProductSelect"></select>
        <label>持仓</label>
        <select id="mp2Metric">
          <option value="long">多头持仓</option>
          <option value="short">空头持仓</option>
          <option value="net">多空净头</option>
          <option value="volume">成交量</option>
        </select>
      </div>
      <svg id="memberPriceChart"></svg>
      <div class="note">左轴：品种结算价（按持仓量加权跨合约平均）；右轴：选定会员在该品种上的持仓。结算价缺夹时仅绘制持仓。</div>
    </div>
    <div class="panel">
      <div class="controls">
        <h2 style="margin-right:auto">最新交易日会员排行</h2>
        <select id="rankProductSelect"></select>
        <select id="rankMetric">
          <option value="volume">成交量</option>
          <option value="long">多头持仓</option>
          <option value="short">空头持仓</option>
        </select>
      </div>
      <svg id="rankChart"></svg>
    </div>
    <div class="panel">
      <h2>最新交易日品种结构</h2>
      <svg id="productChart"></svg>
    </div>
    <div class="panel wide">
      <h2>最新交易日合约持仓 Top 30</h2>
      <div id="contractTable"></div>
      <div class="note">口径：会员排名表内多头持仓 + 空头持仓汇总，不等同于交易所全市场持仓总量。</div>
    </div>
  </section>
</main>
<script>
const DATA = {data_json};
DATA.memberDaily = DATA.memberDaily || [];
const __memberDailyLoaded = new Promise((resolve) => {{
  if (!{sidecar_flag}) {{ resolve(); return; }}
  fetch('member_daily.json', {{cache: 'force-cache'}})
    .then(r => r.ok ? r.json() : [])
    .then(rows => {{ DATA.memberDaily = rows || []; }})
    .catch(() => {{}})
    .finally(() => resolve());
}});
__memberDailyLoaded.then(() => {{ if (typeof renderAll === 'function') renderAll(); }});
const fmt = new Intl.NumberFormat('zh-CN');
const metricLabel = {{volume:'成交量', long:'多头持仓', short:'空头持仓', net_long_short:'多空净额'}};
function num(v) {{ return Number(v || 0); }}
function esc(s) {{
  // Escape DB-derived strings (member/product/contract names) before injecting
  // into innerHTML, to defend against stray HTML/metacharacters.
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }}[c]));
}}
function max(arr, f) {{ return Math.max(1, ...arr.map(f)); }}
function colorFor(i) {{ return ['#2563eb','#059669','#dc2626','#d97706','#7c3aed','#0891b2','#be123c','#4b5563'][i % 8]; }}
function money(v) {{
  const value = num(v);
  if (Math.abs(value) >= 1e12) return `${{(value / 1e12).toFixed(2)}}万亿元`;
  if (Math.abs(value) >= 1e8) return `${{(value / 1e8).toFixed(2)}}亿元`;
  if (Math.abs(value) >= 1e4) return `${{(value / 1e4).toFixed(2)}}万元`;
  return `${{fmt.format(Math.round(value))}}元`;
}}
function displayValue(key, value) {{
  return ['notional_value','estimated_spec_margin','estimated_hedge_margin'].includes(key)
    ? money(value)
    : fmt.format(Math.round(num(value)));
}}
function drawAxes(svg, w, h, pad) {{
  svg.insertAdjacentHTML('beforeend', `<line x1="${{pad.l}}" y1="${{h-pad.b}}" x2="${{w-pad.r}}" y2="${{h-pad.b}}" stroke="#dfe5ee"/><line x1="${{pad.l}}" y1="${{pad.t}}" x2="${{pad.l}}" y2="${{h-pad.b}}" stroke="#dfe5ee"/>`);
}}
function clearSvg(id) {{
  const svg = document.getElementById(id);
  svg.innerHTML = '';
  const box = svg.getBoundingClientRect();
  return [svg, Math.max(320, box.width), 280];
}}
function lineChart(id, data, metric) {{
  const [svg, w, h] = clearSvg(id);
  const moneyMetric = ['notional_value','estimated_spec_margin','estimated_hedge_margin'].includes(metric);
  const pad = {{l:moneyMetric ? 92 : 58,r:18,t:16,b:42}};
  drawAxes(svg,w,h,pad);
  const vals = data.map(d => num(d[metric]));
  const yMin = metric === 'net_long_short' ? Math.min(0, ...vals) : 0;
  const yMax = Math.max(1, ...vals);
  const span = yMax - yMin || 1;
  const x = i => pad.l + (w-pad.l-pad.r) * i / Math.max(1, data.length-1);
  const y = v => h-pad.b - (w && (num(v)-yMin) / span) * (h-pad.t-pad.b);
  const d = data.map((row,i) => `${{i?'L':'M'}} ${{x(i).toFixed(1)}} ${{y(row[metric]).toFixed(1)}}`).join(' ');
  svg.insertAdjacentHTML('beforeend', `<path d="${{d}}" fill="none" stroke="#2563eb" stroke-width="3"/>`);
  data.forEach((row,i) => {{
    if (i % Math.ceil(data.length / 8) === 0 || i === data.length - 1) {{
      svg.insertAdjacentHTML('beforeend', `<text x="${{x(i)}}" y="${{h-16}}" text-anchor="middle" font-size="11" fill="#6b7280">${{row.trade_date.slice(5)}}</text>`);
    }}
  }});
  [yMin, (yMin+yMax)/2, yMax].forEach(v => {{
    svg.insertAdjacentHTML('beforeend', `<text x="${{pad.l-8}}" y="${{y(v)+4}}" text-anchor="end" font-size="11" fill="#6b7280">${{displayValue(metric, v)}}</text>`);
  }});
}}
function memberList() {{
  const latestDate = DATA.daily.length ? DATA.daily[DATA.daily.length - 1].trade_date : '';
  const latestScore = new Map();
  const all = new Set();
  DATA.memberDaily.forEach(row => {{
    if (!row.member) return;
    all.add(row.member);
    if (row.trade_date === latestDate) {{
      latestScore.set(row.member, (latestScore.get(row.member) || 0) + num(row.long) + num(row.short));
    }}
  }});
  return [...all].sort((a, b) => (latestScore.get(b) || 0) - (latestScore.get(a) || 0) || a.localeCompare(b, 'zh-CN'));
}}
function productList() {{
  const names = new Set(DATA.products.map(row => row.product).filter(Boolean));
  return [...names].sort((a, b) => a.localeCompare(b, 'zh-CN'));
}}
function initMemberSelect() {{
  const select = document.getElementById('memberSelect');
  if (select.options.length) return;
  select.innerHTML = memberList().map(member => `<option value="${{esc(member)}}">${{esc(member)}}</option>`).join('');
}}
function initProductSelect(selectId) {{
  const select = document.getElementById(selectId);
  if (select.options.length) return;
  select.innerHTML = `<option value="all">全部品种</option>` + productList().map(product => `<option value="${{product}}">${{product}}</option>`).join('');
}}
function memberSeries(member, metric, product) {{
  const dates = DATA.daily.map(d => d.trade_date);
  const byDate = new Map(dates.map(d => [d, {{ trade_date: d, long: 0, short: 0 }}]));
  DATA.memberDaily.forEach(row => {{
    if (row.member !== member || !byDate.has(row.trade_date)) return;
    if (product !== 'all' && row.product !== product) return;
    byDate.get(row.trade_date).long += num(row.long);
    byDate.get(row.trade_date).short += num(row.short);
  }});
  return dates.map(d => {{
    const item = byDate.get(d);
    item.net = item.long - item.short;
    item.value = item[metric];
    return item;
  }});
}}
function memberLineChart() {{
  const member = document.getElementById('memberSelect').value;
  const product = document.getElementById('memberProductSelect').value;
  const metric = document.getElementById('memberMetric').value;
  const [svg, w, h] = clearSvg('memberChart');
  const data = memberSeries(member, metric, product);
  const pad = {{l:72,r:20,t:18,b:42}};
  drawAxes(svg,w,h,pad);
  const vals = data.map(d => num(d.value));
  const yMin = metric === 'net' ? Math.min(0, ...vals) : 0;
  const yMax = Math.max(1, ...vals);
  const span = yMax - yMin || 1;
  const x = i => pad.l + (w-pad.l-pad.r) * i / Math.max(1, data.length-1);
  const y = v => h-pad.b - (num(v)-yMin) / span * (h-pad.t-pad.b);
  const path = data.map((row,i) => `${{i?'L':'M'}} ${{x(i).toFixed(1)}} ${{y(row.value).toFixed(1)}}`).join(' ');
  svg.insertAdjacentHTML('beforeend', `<path d="${{path}}" fill="none" stroke="#059669" stroke-width="3"/>`);
  data.forEach((row,i) => {{
    const cy = y(row.value);
    svg.insertAdjacentHTML('beforeend', `<circle cx="${{x(i)}}" cy="${{cy}}" r="3" fill="${{row.value === 0 ? '#dc2626' : '#059669'}}"/>`);
    if (i % Math.ceil(data.length / 8) === 0 || i === data.length - 1) {{
      svg.insertAdjacentHTML('beforeend', `<text x="${{x(i)}}" y="${{h-16}}" text-anchor="middle" font-size="11" fill="#6b7280">${{row.trade_date.slice(5)}}</text>`);
    }}
  }});
  [yMin, (yMin+yMax)/2, yMax].forEach(v => {{
    svg.insertAdjacentHTML('beforeend', `<text x="${{pad.l-8}}" y="${{y(v)+4}}" text-anchor="end" font-size="11" fill="#6b7280">${{fmt.format(Math.round(v))}}</text>`);
  }});
  svg.insertAdjacentHTML('beforeend', `<text x="${{pad.l}}" y="12" font-size="12" fill="#374151">${{esc(member)}} · ${{product === 'all' ? '全部品种' : esc(product)}} · ${{metric === 'net' ? '多空净持仓' : metricLabel[metric]}}</text>`);
}}
function rankRows(metric, product) {{
  const totals = new Map();
  DATA.latestMemberProduct.forEach(row => {{
    if (!row.member) return;
    if (product !== 'all' && row.product !== product) return;
    totals.set(row.member, (totals.get(row.member) || 0) + num(row[metric]));
  }});
  return [...totals.entries()]
    .map(([member, value]) => ({{ member, value }}))
    .sort((a, b) => b.value - a.value)
    .slice(0, 30);
}}
function barChart(id, data, valueKey, labelKey) {{
  const [svg, w, h] = clearSvg(id);
  const pad = {{l:96,r:18,t:8,b:20}};
  const rows = data.slice(0, 15);
  const m = max(rows, d => num(d[valueKey]));
  const bh = (h-pad.t-pad.b) / Math.max(1, rows.length);
  rows.forEach((d,i) => {{
    const y = pad.t + i * bh + 3;
    const bw = (w-pad.l-pad.r) * num(d[valueKey]) / m;
    const valueInside = bw > (w-pad.l-pad.r) * .76;
    const valueX = valueInside ? pad.l+bw-5 : pad.l+bw+5;
    const valueAnchor = valueInside ? 'end' : 'start';
    const valueColor = valueInside ? '#fff' : '#6b7280';
    svg.insertAdjacentHTML('beforeend', `<text x="${{pad.l-8}}" y="${{y+bh*.55}}" text-anchor="end" font-size="11" fill="#374151">${{String(d[labelKey]).slice(0,9)}}</text><rect x="${{pad.l}}" y="${{y}}" width="${{bw}}" height="${{Math.max(5,bh-6)}}" fill="${{colorFor(i)}}"/><text x="${{valueX}}" y="${{y+bh*.55}}" text-anchor="${{valueAnchor}}" font-size="11" fill="${{valueColor}}">${{displayValue(valueKey, d[valueKey])}}</text>`);
  }});
}}
function productTreemap(id) {{
  const [svg, w, h] = clearSvg(id);
  const rows = DATA.products.slice(0, 12);
  const total = rows.reduce((s,d)=>s+num(d.open_interest_total),0) || 1;
  let x = 0, y = 0, rowH = h / 3, col = 0;
  rows.forEach((d,i) => {{
    const areaW = Math.max(72, (w * num(d.open_interest_total) / total) * 2.8);
    if (x + areaW > w && x > 0) {{ x = 0; y += rowH; col = 0; }}
    const width = Math.min(areaW, w - x - 4);
    svg.insertAdjacentHTML('beforeend', `<rect x="${{x+2}}" y="${{y+2}}" width="${{Math.max(40,width)}}" height="${{rowH-6}}" rx="4" fill="${{colorFor(i)}}" opacity="0.88"/><text x="${{x+10}}" y="${{y+24}}" fill="#fff" font-size="13" font-weight="700">${{d.product}}</text><text x="${{x+10}}" y="${{y+44}}" fill="#fff" font-size="11">${{fmt.format(num(d.open_interest_total))}}</text>`);
    x += width + 4; col += 1;
  }});
}}
function mp2MemberList() {{
  // Same source as memberList but restricted to members that have ever held a position
  // in any product, so the selector is identical to the existing member chart.
  return memberList();
}}
function mp2ProductList() {{
  // Intersection of products that appear in both member positions and product market data.
  const positionProducts = new Set();
  (DATA.memberDaily || []).forEach(row => {{ if (row.product) positionProducts.add(row.product); }});
  const marketProducts = new Set((DATA.productDailyMarket || []).map(r => r.product).filter(Boolean));
  const common = [...positionProducts].filter(p => marketProducts.has(p));
  return common.sort((a, b) => a.localeCompare(b, 'zh-CN'));
}}
function initMp2MemberSelect() {{
  const select = document.getElementById('mp2MemberSelect');
  if (!select || select.options.length) return;
  const members = mp2MemberList();
  select.innerHTML = members.map(m => `<option value="${{esc(m)}}">${{esc(m)}}</option>`).join('');
}}
function initMp2ProductSelect() {{
  const select = document.getElementById('mp2ProductSelect');
  if (!select) return;
  const products = productList();
  const previous = select.value;
  select.innerHTML = products.map(p => `<option value="${{p}}">${{p}}</option>`).join('');
  if (previous && products.includes(previous)) select.value = previous;
}}
function memberPriceDualChart() {{
  const member = document.getElementById('mp2MemberSelect').value;
  const product = document.getElementById('mp2ProductSelect').value;
  const metric = document.getElementById('mp2Metric').value;
  const [svg, w, h] = clearSvg('memberPriceChart');
  const pad = {{l:72, r:72, t:18, b:42}};

  // Build per-date member position series (filtered by product).
  const memberRows = (DATA.memberDaily || []).filter(r => r.member === member && r.product === product);
  const byDate = new Map(memberRows.map(r => [r.trade_date, r]));
  // Build per-date product settlement price map.
  const priceRows = (DATA.productDailyMarket || []).filter(r => r.product === product || r.product_code === product);
  const priceByDate = new Map(priceRows.map(r => [r.trade_date, r.settlement_price]));

  // Union of dates sorted ascending.
  const allDates = [...new Set([...byDate.keys(), ...priceByDate.keys()])].sort();
  if (!allDates.length) {{
    svg.insertAdjacentHTML('beforeend', `<text x="${{w/2}}" y="${{h/2}}" text-anchor="middle" fill="#6b7280">无数据</text>`);
    return;
  }}

  const positions = allDates.map(d => {{
    const row = byDate.get(d);
    if (!row) return 0;
    return num(row[metric]);
  }});
  const prices = allDates.map(d => {{
    const v = priceByDate.get(d);
    return v == null ? null : num(v);
  }});

  // Y ranges
  const posMax = Math.max(1, ...positions.map(Math.abs));
  const posMin = metric === 'net' ? -posMax : 0;
  const posSpan = (posMax - posMin) || 1;
  const validPrices = prices.filter(p => p != null);
  const priceMax = validPrices.length ? Math.max(...validPrices) : 1;
  const priceMin = validPrices.length ? Math.min(...validPrices) : 0;
  // Add 5% padding so lines don't kiss the edges.
  const pricePad = (priceMax - priceMin) * 0.05 || priceMax * 0.05 || 1;
  const priceLo = priceMin - pricePad;
  const priceHi = priceMax + pricePad;
  const priceSpan = (priceHi - priceLo) || 1;

  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const x = i => pad.l + innerW * i / Math.max(1, allDates.length - 1);
  const yL = v => pad.t + innerH * (1 - (v - priceLo) / priceSpan);
  const yR = v => pad.t + innerH * (1 - (v - posMin) / posSpan);

  // Axes
  // Left axis (settlement price)
  for (const v of [priceLo, (priceLo + priceHi) / 2, priceHi]) {{
    const yy = yL(v);
    svg.insertAdjacentHTML('beforeend', `<line x1="${{pad.l}}" y1="${{yy}}" x2="${{w - pad.r}}" y2="${{yy}}" stroke="#e5e7eb" stroke-dasharray="3 3"/>`);
    svg.insertAdjacentHTML('beforeend', `<text x="${{pad.l - 8}}" y="${{yy + 4}}" text-anchor="end" font-size="11" fill="#2563eb">${{fmt.format(Math.round(v))}}</text>`);
  }}
  // Right axis (position)
  for (const v of [posMin, (posMin + posMax) / 2, posMax]) {{
    const yy = yR(v);
    svg.insertAdjacentHTML('beforeend', `<text x="${{w - pad.r + 8}}" y="${{yy + 4}}" text-anchor="start" font-size="11" fill="#059669">${{fmt.format(Math.round(v))}}</text>`);
  }}
  // X axis labels (sparse)
  const step = Math.max(1, Math.floor(allDates.length / 8));
  allDates.forEach((d, i) => {{
    if (i % step === 0 || i === allDates.length - 1) {{
      svg.insertAdjacentHTML('beforeend', `<text x="${{x(i)}}" y="${{h - 16}}" text-anchor="middle" font-size="11" fill="#6b7280">${{d.slice(5)}}</text>`);
    }}
  }});

  // Settlement price line (left axis, blue)
  let path = '';
  let started = false;
  prices.forEach((p, i) => {{
    if (p == null) {{ started = false; return; }}
    const cmd = started ? 'L' : 'M';
    path += `${{cmd}} ${{x(i).toFixed(1)}} ${{yL(p).toFixed(1)}} `;
    started = true;
  }});
  if (path) {{
    svg.insertAdjacentHTML('beforeend', `<path d="${{path.trim()}}" fill="none" stroke="#2563eb" stroke-width="2.5"/>`);
  }}

  // Position bars (right axis, green/red depending on sign)
  const barW = Math.max(1.5, innerW / Math.max(1, allDates.length) * 0.6);
  positions.forEach((v, i) => {{
    const cx = x(i);
    const zero = yR(0);
    const top = yR(v);
    const fill = v >= 0 ? '#059669' : '#dc2626';
    const yTop = Math.min(top, zero);
    const height = Math.max(0.5, Math.abs(top - zero));
    svg.insertAdjacentHTML('beforeend', `<rect x="${{(cx - barW/2).toFixed(1)}}" y="${{yTop.toFixed(1)}}" width="${{barW.toFixed(1)}}" height="${{height.toFixed(1)}}" fill="${{fill}}" opacity="0.75"/>`);
  }});

  // Legend
  svg.insertAdjacentHTML('beforeend', `<g transform="translate(${{pad.l + 8}}, ${{pad.t + 4}})">
    <rect x="0" y="0" width="14" height="3" fill="#2563eb"/><text x="20" y="5" font-size="11" fill="#172033">结算价（左轴）</text>
    <rect x="120" y="-3" width="10" height="9" fill="#059669" opacity="0.75"/><text x="135" y="5" font-size="11" fill="#172033">持仓（右轴）</text>
  </g>`);
}}
function renderTable() {{
  const rows = DATA.contracts.slice(0, 30);
  document.getElementById('contractTable').innerHTML = `<table><thead><tr><th>品种</th><th>合约</th><th>成交量</th><th>多头持仓</th><th>空头持仓</th><th>多空合计</th></tr></thead><tbody>${{rows.map(d => `<tr><td>${{esc(d.product)}}</td><td>${{esc(d.contract)}}</td><td>${{fmt.format(num(d.volume))}}</td><td>${{fmt.format(num(d.long))}}</td><td>${{fmt.format(num(d.short))}}</td><td>${{fmt.format(num(d.open_interest_total))}}</td></tr>`).join('')}}</tbody></table>`;
}}
function renderMarketTable() {{
  const rows = (DATA.marketContracts || []).slice(0, 30);
  document.getElementById('marketContractTable').innerHTML = `<table><thead><tr><th>品种</th><th>合约</th><th>结算价</th><th>交易所持仓量</th><th>合约乘数</th><th>名义持仓市值</th><th>投机保证金估算</th></tr></thead><tbody>${{rows.map(d => `<tr><td>${{esc(d.product)}}</td><td>${{esc(d.contract)}}</td><td>${{fmt.format(num(d.settlement_price))}}</td><td>${{fmt.format(num(d.open_interest))}}</td><td>${{fmt.format(num(d.contract_multiplier))}} ${{esc(d.multiplier_unit || '')}}</td><td>${{money(d.notional_value)}}</td><td>${{money(d.estimated_spec_margin)}}</td></tr>`).join('')}}</tbody></table>`;
}}
function renderKpis() {{
  const daily = DATA.daily;
  const latest = daily[daily.length - 1] || {{}};
  const marketDaily = DATA.marketDaily || [];
  const latestMarket = marketDaily[marketDaily.length - 1] || {{}};
  const topVol = DATA.members.filter(d => d.metric === 'volume')[0] || {{}};
  const cards = [
    ['覆盖交易日', daily.length],
    ['最新日期', latest.trade_date || '-'],
    ['全市场名义持仓市值', money(latestMarket.notional_value)],
    ['投机保证金估算（双边）', money(latestMarket.estimated_spec_margin)],
    ['交易所总持仓量', fmt.format(num(latestMarket.open_interest))],
    ['成交量首位会员', topVol.member || '-'],
  ];
  document.getElementById('kpis').innerHTML = cards.map(c => `<div class="card"><div class="label">${{esc(c[0])}}</div><div class="value">${{esc(c[1])}}</div></div>`).join('');
}}
function renderAll() {{
  renderKpis();
  initMemberSelect();
  initProductSelect('memberProductSelect');
  initProductSelect('rankProductSelect');
  lineChart('marketTrendChart', DATA.marketDaily || [], document.getElementById('marketTrendMetric').value);
  barChart('marketProductChart', DATA.marketProducts || [], 'notional_value', 'product');
  barChart('marketMarginChart', DATA.marketProducts || [], 'estimated_spec_margin', 'product');
  lineChart('trendChart', DATA.daily, document.getElementById('trendMetric').value);
  initMp2MemberSelect();
  initMp2ProductSelect();
  initReportProductSelect();
  memberPriceDualChart();
  memberLineChart();
  const metric = document.getElementById('rankMetric').value;
  const rankProduct = document.getElementById('rankProductSelect').value;
  barChart('rankChart', rankRows(metric, rankProduct), 'value', 'member');
  productTreemap('productChart');
  renderMarketTable();
  renderTable();
}}
document.getElementById('marketTrendMetric').addEventListener('change', renderAll);
document.getElementById('trendMetric').addEventListener('change', renderAll);
document.getElementById('memberSelect').addEventListener('change', renderAll);
document.getElementById('memberProductSelect').addEventListener('change', renderAll);
document.getElementById('memberMetric').addEventListener('change', renderAll);
document.getElementById('rankProductSelect').addEventListener('change', renderAll);
document.getElementById('rankMetric').addEventListener('change', renderAll);
document.getElementById('mp2MemberSelect').addEventListener('change', memberPriceDualChart);
document.getElementById('mp2ProductSelect').addEventListener('change', memberPriceDualChart);
document.getElementById('mp2Metric').addEventListener('change', memberPriceDualChart);
window.addEventListener('resize', () => setTimeout(renderAll, 80));

// === AI report modal ===
// === Product combobox (searchable dropdown) ===
// State container shared by init/close/listener handlers.
const productCombobox = (() => {{
  const state = {{
    products: [],
    filtered: [],
    activeIndex: -1,
    open: false,
    selectedValue: '',
    filterText: '',
    bound: false,
    inputEl: null,
    hiddenEl: null,
    dropdownEl: null,
  }};

  function escapeHtml(s) {{
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }}[c]));
  }}

  function highlight(text, query) {{
    if (!query) return escapeHtml(text);
    const idx = text.indexOf(query);
    if (idx < 0) return escapeHtml(text);
    return escapeHtml(text.slice(0, idx))
      + '<mark>' + escapeHtml(text.slice(idx, idx + query.length)) + '</mark>'
      + escapeHtml(text.slice(idx + query.length));
  }}

  function renderDropdown() {{
    if (!state.dropdownEl) return;
    const query = state.filterText;
    state.filtered = state.products.filter(p => !query || p.toLowerCase().includes(query));
    state.activeIndex = state.filtered.length ? 0 : -1;
    if (!state.filtered.length) {{
      state.dropdownEl.innerHTML = `<div class="combobox-option no-match">无匹配品种：${{escapeHtml(state.inputEl.value || '')}}</div>`;
    }} else {{
      state.dropdownEl.innerHTML = state.filtered.map((p, i) => {{
        const cls = i === 0 ? 'combobox-option active' : 'combobox-option';
        return `<div class="${{cls}}" data-value="${{escapeHtml(p)}}" data-index="${{i}}">${{highlight(p, query)}}</div>`;
      }}).join('');
    }}
  }}

  function open() {{
    if (state.open) return;
    state.open = true;
    state.filterText = '';
    state.dropdownEl.classList.add('open');
    renderDropdown();
  }}

  function close() {{
    if (!state.open) return;
    state.open = false;
    state.dropdownEl.classList.remove('open');
  }}

  function selectValue(value) {{
    state.selectedValue = value;
    state.inputEl.value = value;
    state.hiddenEl.value = value;
    state.filterText = '';
    close();
    // Notify listeners (runReport reads hiddenEl.value).
    state.inputEl.dispatchEvent(new Event('change', {{ bubbles: true }}));
  }}

  function moveActive(delta) {{
    if (!state.filtered.length) return;
    state.activeIndex = (state.activeIndex + delta + state.filtered.length) % state.filtered.length;
    const items = state.dropdownEl.querySelectorAll('.combobox-option[data-value]');
    items.forEach((el, i) => el.classList.toggle('active', i === state.activeIndex));
    const cur = items[state.activeIndex];
    if (cur) cur.scrollIntoView({{ block: 'nearest' }});
  }}

  function bind() {{
    state.inputEl = document.getElementById('reportProductInput');
    state.hiddenEl = document.getElementById('reportProductSelect');
    state.dropdownEl = document.getElementById('reportProductDropdown');
    if (!state.inputEl || !state.hiddenEl || !state.dropdownEl) return false;
    if (state.bound) return true;  // prevent duplicate listeners on re-init
    state.bound = true;

    state.inputEl.addEventListener('focus', () => {{
      // Refresh product list lazily on focus so newly built dashboards stay in sync.
      state.products = productList();
      // Default-select the first product if hidden value is empty.
      if (!state.hiddenEl.value && state.products.length) {{
        state.hiddenEl.value = state.products[0];
        state.inputEl.value = state.products[0];
        state.selectedValue = state.products[0];
      }}
      open();
    }});

    state.inputEl.addEventListener('input', () => {{
      // Typing clears the committed value; user must pick a known product to submit.
      state.hiddenEl.value = '';
      state.filterText = (state.inputEl.value || '').trim().toLowerCase();
      if (!state.open) open();
      else renderDropdown();
    }});

    state.inputEl.addEventListener('keydown', (e) => {{
      if (e.key === 'ArrowDown') {{ e.preventDefault(); moveActive(1); }}
      else if (e.key === 'ArrowUp') {{ e.preventDefault(); moveActive(-1); }}
      else if (e.key === 'Enter') {{
        e.preventDefault();
        if (state.open && state.activeIndex >= 0 && state.filtered[state.activeIndex]) {{
          selectValue(state.filtered[state.activeIndex]);
        }}
      }} else if (e.key === 'Escape') {{
        close();
        // Restore the previously committed value on Esc.
        state.inputEl.value = state.selectedValue;
        state.hiddenEl.value = state.selectedValue;
        state.filterText = '';
      }} else if (e.key === 'Tab') {{
        // Auto-pick the top match on Tab so the user can move to next control.
        if (state.open && state.filtered.length) {{
          selectValue(state.filtered[0]);
        }}
      }}
    }});

    state.dropdownEl.addEventListener('click', (e) => {{
      const target = e.target.closest('.combobox-option[data-value]');
      if (target) selectValue(target.getAttribute('data-value'));
    }});

    // Click outside -> close.
    document.addEventListener('click', (e) => {{
      const box = document.getElementById('reportProductCombobox');
      if (state.open && box && !box.contains(e.target)) close();
    }});
    return true;
  }}

  // Public API.
  return {{
    init() {{
      state.products = productList();
      if (bind() && state.products.length) {{
        state.selectedValue = state.products[0];
        state.inputEl.value = state.products[0];
        state.hiddenEl.value = state.products[0];
      }}
    }},
    getValue() {{ return state.hiddenEl.value; }},
    setProducts(list) {{ state.products = list || []; }},
  }};
}})();

function initReportProductSelect() {{
  // Defer to the IIFE-backed controller; safe to call multiple times.
  productCombobox.init();
}}
function fmtInt(v) {{
  const n = num(v);
  return fmt.format(Math.round(n));
}}
function fmtDelta(v) {{
  if (v == null) return '—';
  const n = num(v);
  const sign = n > 0 ? '+' : '';
  return sign + fmt.format(Math.round(n));
}}
function renderReportSummary(summary) {{
  const days = summary.days_data || [];
  const rows = days.map(d => {{
    const topLong = (d.top_long || []).map(r => `${{esc(r.member)}}(${{fmtInt(r.value)}})`).join('; ');
    const topShort = (d.top_short || []).map(r => `${{esc(r.member)}}(${{fmtInt(r.value)}})`).join('; ');
    return `<tr>
      <td>${{d.trade_date}}</td>
      <td>${{fmtInt(d.long_total)}}</td>
      <td>${{fmtDelta(d.change_long)}}</td>
      <td>${{fmtInt(d.short_total)}}</td>
      <td>${{fmtDelta(d.change_short)}}</td>
      <td>${{fmtInt(d.net_long_short)}}</td>
      <td>${{fmtDelta(d.change_net)}}</td>
      <td style="max-width:280px;font-size:11px">${{topLong}}</td>
      <td style="max-width:280px;font-size:11px">${{topShort}}</td>
    </tr>`;
  }}).join('');
  return `
    <div class="modal-section">
      <h3>多空总持仓与净头的日度变化（前 5 席位汇总）</h3>
      <table>
        <thead><tr>
          <th>交易日</th>
          <th>多头总计</th><th>多头变化</th>
          <th>空头总计</th><th>空头变化</th>
          <th>净多空</th><th>净头变化</th>
          <th>多头 Top5</th>
          <th>空头 Top5</th>
        </tr></thead>
        <tbody>${{rows}}</tbody>
      </table>
    </div>
  `;
}}
function openReportModal() {{
  document.getElementById('reportModal').classList.add('open');
  document.body.style.overflow = 'hidden';
}}
function closeReportModal() {{
  document.getElementById('reportModal').classList.remove('open');
  document.body.style.overflow = '';
}}
// === Report: opens a new window with the full report page ===
async function runReport() {{
  const product = productCombobox.getValue();
  const days = document.getElementById('reportDaysSelect').value;
  const btn = document.getElementById('reportBtn');
  if (!product) {{
    alert('请先选择品种');
    return;
  }}
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = '生成中...';
  // Open the report page in a new window.
  // The Python endpoint handles aggregation + DeepSeek call + HTML rendering.
  const url = `/report/page?product=${{encodeURIComponent(product)}}&days=${{days}}`;
  const reportWin = window.open(url, '_blank');
  if (!reportWin) {{
    // Popup blocked: show a link in the modal as fallback.
    const statusEl = document.getElementById('reportStatus');
    const bodyEl = document.getElementById('reportBody');
    const titleEl = document.getElementById('reportTitle');
    titleEl.textContent = `AI 报表：${{product}} 近 ${{days}} 日`;
    statusEl.textContent = '弹窗被浏览器拦截，请点击下方链接打开报表：';
    bodyEl.innerHTML = `<div class="modal-section"><a href="${{url}}" target="_blank" style="font-size:16px;color:#2563eb">打开 AI 报表：${{product}} 近 ${{days}} 日</a></div>`;
    openReportModal();
  }}
  btn.disabled = false;
  btn.textContent = originalLabel;
}}
document.getElementById('reportBtn').addEventListener('click', runReport);
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') closeReportModal();
}});
renderAll();
</script>
<div class="modal-backdrop" id="reportModal" onclick="if(event.target===this)closeReportModal()">
  <div class="modal">
    <div class="modal-header">
      <h2 id="reportTitle">AI 报表</h2>
      <button type="button" onclick="closeReportModal()" aria-label="关闭">×</button>
    </div>
    <div id="reportStatus" class="report-status"></div>
    <div id="reportBody"></div>
  </div>
</div>
</body>
</html>"""
    return out_html, sidecar_json


def main() -> None:
    args = parse_args()
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else None
    normalized, log = collect_shfe_range(args.days, end_date, args.timeout)
    summaries = build_summary(normalized)
    paths = export_workbook(Path(args.output_dir), normalized, log, summaries)
    print(f"采集自然日: {args.days}")
    print(f"成功交易日: {(log['status'] == 'ok').sum() if not log.empty else 0}")
    print(f"明细行数: {len(normalized)}")
    print(f"CSV: {paths['csv']}")
    print(f"Excel: {paths['excel']}")
    print(f"HTML: {paths['html']}")


if __name__ == "__main__":
    main()
