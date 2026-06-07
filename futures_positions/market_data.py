from __future__ import annotations

import json
import re
from datetime import date
from xml.etree import ElementTree

import pandas as pd
import requests

from .utils import clean_number, fetched_at, get_text, yyyymmdd


DAILY_MARKET_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{date}.dat"
SETTLEMENT_PARAMS_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/js{date}.dat"
SPECS_SOURCE_URL = "https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/"

INE_PRODUCTS = {"bc", "sc", "nr", "lu", "ec"}

# CFFEX (China Financial Futures Exchange) -- extension phase
# Only financial futures; equity index options IO/MO/HO are excluded from
# daily market collection (settlement-price series not published in rtj).
CFFEX_FUTURE_PRODUCTS = {"IF", "IC", "IM", "IH", "TS", "TF", "T", "TL"}

CFFEX_RTJ_URL = "http://www.cffex.com.cn/sj/hqsj/rtj/{ym}/{day}/index.xml"
CFFEX_SPECS_SOURCE_URL = "http://www.cffex.com.cn/cn/cpzl/index.html"

CFFEX_SPECS = {
    "IF": ("沪深300", 200, "元/点"),
    "IC": ("中证500", 200, "元/点"),
    "IM": ("中证1000", 200, "元/点"),
    "IH": ("上证50", 300, "元/点"),
    "TS": ("2年期国债", 20000, "元/手"),
    "TF": ("5年期国债", 10000, "元/手"),
    "T":  ("10年期国债", 10000, "元/手"),
    "TL": ("30年期国债", 10000, "元/手"),
}

# Numeric multipliers are verified against the turnover and volume published in
# SHFE's daily market file. The effective-date schema allows future changes to
# be added without rewriting historical calculations.
# CFFEX exchange-level (通用/交易所) margin rates. CFFEX does not publish
# these in the rtj daily-stats feed, so we fall back to the standard
# exchange-published baseline values. These do NOT include futures-company
# add-ons or combination discounts. Values are expressed as fractions (0.12
# means 12%). Spec and hedge rates are kept equal per CFFEX's current
# exchange-level baseline.
CFFEX_DEFAULT_MARGIN_RATES = {
    "IF": {"spec_long": 0.12, "spec_short": 0.12, "hedge_long": 0.12, "hedge_short": 0.12},
    "IH": {"spec_long": 0.12, "spec_short": 0.12, "hedge_long": 0.12, "hedge_short": 0.12},
    "IC": {"spec_long": 0.14, "spec_short": 0.14, "hedge_long": 0.14, "hedge_short": 0.14},
    "IM": {"spec_long": 0.14, "spec_short": 0.14, "hedge_long": 0.14, "hedge_short": 0.14},
    "TS": {"spec_long": 0.005, "spec_short": 0.005, "hedge_long": 0.005, "hedge_short": 0.005},
    "TF": {"spec_long": 0.01, "spec_short": 0.01, "hedge_long": 0.01, "hedge_short": 0.01},
    "T":  {"spec_long": 0.02, "spec_short": 0.02, "hedge_long": 0.02, "hedge_short": 0.02},
    "TL": {"spec_long": 0.035, "spec_short": 0.035, "hedge_long": 0.035, "hedge_short": 0.035},
}

# CZCE (Zhengzhou Commodity Exchange) -- extension phase.
# Per-product specs (name, multiplier, unit). Multipliers/units verified
# against CZCE contract info. CZCE publishes a static daily xlsx per trading
# day under http://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{YYYYMMDD}/
# (note: http not https, which returns 412; .xlsx not .htm/.txt).
CZCE_SPECS_SOURCE_URL = "http://www.czce.com.cn/cn/jysj/"
CZCE_FUTURE_PRODUCTS = {
    "AP", "CF", "CJ", "CY", "FG", "JR", "LR", "MA", "OI", "PF", "PK", "PL",
    "PM", "PR", "PX", "RI", "RM", "RS", "SA", "SF", "SH", "SM", "SR", "TA",
    "UR", "WH", "ZC",
}
CZCE_SPECS = {
    "AP": ("苹果", 10, "吨/手"),
    "CF": ("棉花", 5, "吨/手"),
    "CJ": ("红枣", 5, "吨/手"),
    "CY": ("棉纱", 5, "吨/手"),
    "FG": ("玻璃", 20, "吨/手"),
    "JR": ("粳米", 20, "吨/手"),
    "LR": ("晚籼稻", 20, "吨/手"),
    "MA": ("甲醇", 10, "吨/手"),
    "OI": ("菜油", 10, "吨/手"),
    "PF": ("短纤", 5, "吨/手"),
    "PK": ("花生", 5, "吨/手"),
    "PL": ("瓶片", 5, "吨/手"),
    "PM": ("普麦", 50, "吨/手"),
    "PR": ("瓶片(PR)", 20, "吨/手"),
    "PX": ("对二甲苯", 5, "吨/手"),
    "RI": ("早籼稻", 20, "吨/手"),
    "RM": ("菜粕", 10, "吨/手"),
    "RS": ("菜籽", 10, "吨/手"),
    "SA": ("纯碱", 20, "吨/手"),
    "SF": ("硅铁", 5, "吨/手"),
    "SH": ("烧碱", 30, "吨/手"),
    "SM": ("锰硅", 5, "吨/手"),
    "SR": ("白糖", 10, "吨/手"),
    "TA": ("PTA", 5, "吨/手"),
    "UR": ("尿素", 20, "吨/手"),
    "WH": ("强麦", 20, "吨/手"),
    "ZC": ("动力煤", 100, "吨/手"),
}

# CZCE exchange-level (投机/套保) margin rates, expressed as fractions.
# Verified against CZCE contract-info feed. Note these are the exchange
# baselines; futures-company add-ons are not included. Spec == hedge per
# CZCE's current exchange-level baseline.
CZCE_DEFAULT_MARGIN_RATES = {
    "AP": 0.13, "CF": 0.10, "CJ": 0.15, "CY": 0.10, "FG": 0.12, "JR": 0.15,
    "LR": 0.15, "MA": 0.10, "OI": 0.10, "PF": 0.10, "PK": 0.10, "PL": 0.12,
    "PM": 0.15, "PR": 0.12, "PX": 0.12, "RI": 0.15, "RM": 0.10, "RS": 0.20,
    "SA": 0.12, "SF": 0.10, "SH": 0.12, "SM": 0.10, "SR": 0.10, "TA": 0.10,
    "UR": 0.11, "WH": 0.15, "ZC": 0.50,
}

CONTRACT_SPECS = {
    "cu": ("铜", 5, "吨/手"),
    "bc": ("铜(BC)", 5, "吨/手"),
    "al": ("铝", 5, "吨/手"),
    "zn": ("锌", 5, "吨/手"),
    "pb": ("铅", 5, "吨/手"),
    "ni": ("镍", 1, "吨/手"),
    "sn": ("锡", 1, "吨/手"),
    "au": ("黄金", 1000, "克/手"),
    "ag": ("白银", 15, "千克/手"),
    "rb": ("螺纹钢", 10, "吨/手"),
    "wr": ("线材", 10, "吨/手"),
    "hc": ("热轧卷板", 10, "吨/手"),
    "ss": ("不锈钢", 5, "吨/手"),
    "ao": ("氧化铝", 20, "吨/手"),
    "ad": ("铸造铝合金", 10, "吨/手"),
    "ru": ("天然橡胶", 10, "吨/手"),
    "br": ("丁二烯橡胶", 5, "吨/手"),
    "bu": ("石油沥青", 10, "吨/手"),
    "fu": ("燃料油", 10, "吨/手"),
    "sp": ("纸浆", 10, "吨/手"),
    "op": ("胶版印刷纸", 40, "吨/手"),
    "sc": ("原油", 1000, "桶/手"),
    "nr": ("20号胶", 10, "吨/手"),
    "lu": ("低硫燃料油", 10, "吨/手"),
    "ec": ("SCFIS欧线", 50, "元/指数点"),
}

MARKET_COLUMNS = [
    "trade_date",
    "exchange",
    "product_code",
    "product_name",
    "contract",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "settlement_price",
    "pre_settlement_price",
    "volume",
    "open_interest",
    "open_interest_change",
    "turnover",
    "source_url",
    "fetched_at",
]

SETTLEMENT_COLUMNS = [
    "trade_date",
    "exchange",
    "product_code",
    "contract",
    "settlement_price",
    "spec_long_margin_rate",
    "spec_short_margin_rate",
    "hedge_long_margin_rate",
    "hedge_short_margin_rate",
    "trade_fee_ratio",
    "close_today_fee_ratio",
    "source_url",
    "fetched_at",
]


def exchange_for_product(product_code: str) -> str:
    """Return the exchange for a product code.

    SHFE/INE codes are lowercase in CONTRACT_SPECS; CFFEX codes are uppercase
    in CFFEX_SPECS. Handle both cases gracefully.
    """
    if not product_code:
        return "SHFE"
    code = product_code.lower()
    if code in INE_PRODUCTS:
        return "INE"
    code_up = product_code.upper()
    if code_up in CFFEX_FUTURE_PRODUCTS:
        return "CFFEX"
    if code_up in CZCE_FUTURE_PRODUCTS:
        return "CZCE"
    return "SHFE"


def contract_spec_rows() -> list[dict]:
    at = fetched_at()
    rows: list[dict] = []
    for code, (name, multiplier, unit) in CONTRACT_SPECS.items():
        rows.append(
            {
                "exchange": exchange_for_product(code),
                "product_code": code,
                "product_name": name,
                "contract_multiplier": multiplier,
                "multiplier_unit": unit,
                "effective_from": "2000-01-01",
                "effective_to": None,
                "source_url": SPECS_SOURCE_URL,
                "updated_at": at,
            }
        )
    for code, (name, multiplier, unit) in CFFEX_SPECS.items():
        rows.append(
            {
                "exchange": "CFFEX",
                "product_code": code,
                "product_name": name,
                "contract_multiplier": multiplier,
                "multiplier_unit": unit,
                "effective_from": "2000-01-01",
                "effective_to": None,
                "source_url": CFFEX_SPECS_SOURCE_URL,
                "updated_at": at,
            }
        )
    for code, (name, multiplier, unit) in CZCE_SPECS.items():
        rows.append(
            {
                "exchange": "CZCE",
                "product_code": code,
                "product_name": name,
                "contract_multiplier": multiplier,
                "multiplier_unit": unit,
                "effective_from": "2000-01-01",
                "effective_to": None,
                "source_url": CZCE_SPECS_SOURCE_URL,
                "updated_at": at,
            }
        )
    return rows


def parse_daily_market(payload: dict, trade_date: date, source_url: str) -> pd.DataFrame:
    records = []
    at = fetched_at()
    for row in payload.get("o_curinstrument", []):
        product_code = str(row.get("PRODUCTGROUPID") or "").strip().lower()
        delivery_month = str(row.get("DELIVERYMONTH") or "").strip()
        if not product_code or product_code == "sc_tas" or not re.fullmatch(r"\d{4}", delivery_month):
            continue
        spec = CONTRACT_SPECS.get(product_code)
        if spec is None:
            continue
        records.append(
            {
                "trade_date": trade_date.isoformat(),
                "exchange": exchange_for_product(product_code),
                "product_code": product_code,
                "product_name": spec[0],
                "contract": f"{product_code}{delivery_month}",
                "open_price": clean_number(row.get("OPENPRICE")),
                "high_price": clean_number(row.get("HIGHESTPRICE")),
                "low_price": clean_number(row.get("LOWESTPRICE")),
                "close_price": clean_number(row.get("CLOSEPRICE")),
                "settlement_price": clean_number(row.get("SETTLEMENTPRICE")),
                "pre_settlement_price": clean_number(row.get("PRESETTLEMENTPRICE")),
                "volume": clean_number(row.get("VOLUME")),
                "open_interest": clean_number(row.get("OPENINTEREST")),
                "open_interest_change": clean_number(row.get("OPENINTERESTCHG")),
                "turnover": clean_number(row.get("TURNOVER")),
                "source_url": source_url,
                "fetched_at": at,
            }
        )
    return pd.DataFrame(records, columns=MARKET_COLUMNS)


def parse_settlement_params(payload: dict, trade_date: date, source_url: str) -> pd.DataFrame:
    records = []
    at = fetched_at()
    for row in payload.get("o_cursor", []):
        contract = str(row.get("INSTRUMENTID") or "").strip().lower()
        match = re.fullmatch(r"([a-z]+)\d{4}", contract)
        if not match:
            continue
        product_code = match.group(1)
        if product_code not in CONTRACT_SPECS:
            continue
        records.append(
            {
                "trade_date": trade_date.isoformat(),
                "exchange": exchange_for_product(product_code),
                "product_code": product_code,
                "contract": contract,
                "settlement_price": clean_number(row.get("SETTLEMENTPRICE")),
                "spec_long_margin_rate": clean_number(row.get("SPECLONGMARGINRATIO")),
                "spec_short_margin_rate": clean_number(row.get("SPECSHORTMARGINRATIO")),
                "hedge_long_margin_rate": clean_number(row.get("HEDGLONGMARGINRATIO")),
                "hedge_short_margin_rate": clean_number(row.get("HEDGSHORTMARGINRATIO")),
                "trade_fee_ratio": clean_number(row.get("TRADEFEERATIO")),
                "close_today_fee_ratio": clean_number(row.get("TTRADEFEERATIO")),
                "source_url": source_url,
                "fetched_at": at,
            }
        )
    return pd.DataFrame(records, columns=SETTLEMENT_COLUMNS)


def fetch_daily_market(
    trade_date: date,
    http: requests.Session,
) -> tuple[pd.DataFrame, pd.DataFrame, str, str]:
    tag = yyyymmdd(trade_date)
    market_url = DAILY_MARKET_URL.format(date=tag)
    settlement_url = SETTLEMENT_PARAMS_URL.format(date=tag)
    market_payload = json.loads(get_text(http, market_url, encoding="utf-8"))
    settlement_payload = json.loads(get_text(http, settlement_url, encoding="utf-8"))
    return (
        parse_daily_market(market_payload, trade_date, market_url),
        parse_settlement_params(settlement_payload, trade_date, settlement_url),
        market_url,
        settlement_url,
    )


def _oi_change(node) -> float | None:
    """Compute CFFEX open-interest change from rtj when delta tag is empty.

    The exchange publishes <delta> for options only. For futures we derive
    OI change from ``openinterest - preopeninterest`` (both reliably set).
    """
    delta = clean_number(node.findtext("delta"))
    if delta is not None:
        return delta
    oi = clean_number(node.findtext("openinterest"))
    pre = clean_number(node.findtext("preopeninterest"))
    if oi is not None and pre is not None:
        return oi - pre
    return None


def parse_cffex_rtj_xml(text: str, trade_date: date, source_url: str) -> pd.DataFrame:
    """Parse CFFEX daily statistics XML (rtj) into MARKET_COLUMNS schema.

    The rtj feed publishes one <dailydata> element per contract. Fields map
    almost 1:1 to MARKET_COLUMNS. Options (contracts containing "-") are
    dropped because product_specs and the dashboard expect futures only.
    """
    records: list[dict] = []
    at = fetched_at()
    root = ElementTree.fromstring(text)
    for node in root.iter("dailydata"):
        contract = (node.findtext("instrumentid") or "").strip()
        product_code = (node.findtext("productid") or "").strip().upper()
        if not contract or not product_code:
            continue
        if "-" in contract or product_code not in CFFEX_FUTURE_PRODUCTS:
            # Skip options (e.g. IO2506-C-4200) and unknown products
            continue
        spec = CFFEX_SPECS.get(product_code)
        if spec is None:
            continue
        records.append(
            {
                "trade_date": trade_date.isoformat(),
                "exchange": "CFFEX",
                "product_code": product_code,
                "product_name": spec[0],
                "contract": contract,
                "open_price": clean_number(node.findtext("openprice")),
                "high_price": clean_number(node.findtext("highestprice")),
                "low_price": clean_number(node.findtext("lowestprice")),
                "close_price": clean_number(node.findtext("closeprice")),
                "settlement_price": clean_number(node.findtext("settlementprice")),
                "pre_settlement_price": clean_number(node.findtext("presettlementprice")),
                "volume": clean_number(node.findtext("volume")),
                "open_interest": clean_number(node.findtext("openinterest")),
                "open_interest_change": _oi_change(node),
                "turnover": clean_number(node.findtext("turnover")),
                "source_url": source_url,
                "fetched_at": at,
            }
        )
    return pd.DataFrame(records, columns=MARKET_COLUMNS)


def parse_cffex_position_xml(text: str, trade_date: date, product: str, source_url: str) -> pd.DataFrame:
    """Parse CFFEX ccpm rank XML for a single product.

    Each <data> row carries datatypeid (0=volume, 1=long, 2=short), rank,
    shortname (member), volume and varvolume (change). Returns the standard
    positions-frame schema (POSITION_COLUMNS) with exchange="CFFEX".
    """
    from .database import POSITION_COLUMNS  # local import avoids cycle

    metric_map = {"0": "volume", "1": "long", "2": "short"}
    records: list[dict] = []
    at = fetched_at()
    root = ElementTree.fromstring(text)
    for node in root.iter("data"):
        # Only treat leaf-like <data> rows carrying a rank child
        rank_text = node.findtext("rank")
        if rank_text is None:
            continue
        rank = clean_number(rank_text)
        if rank is None or not (1 <= int(rank) <= 20):
            continue
        metric = metric_map.get((node.findtext("datatypeid") or "").strip())
        if metric is None:
            continue
        member = (node.findtext("shortname") or "").strip()
        # Strip the "(代客)" / "（代客）" suffix that CFFEX appends to
        # client-trading rows so member names match across exchanges.
        member = __strip_agent_suffix(member)
        if not member:
            continue
        contract = (node.findtext("instrumentid") or "").strip()
        records.append(
            {
                "trade_date": trade_date.isoformat(),
                "exchange": "CFFEX",
                "product": product,
                "contract": contract,
                "rank": int(rank),
                "metric": metric,
                "member": member,
                "value": clean_number(node.findtext("volume")),
                "change": clean_number(node.findtext("varvolume")),
                "source_url": source_url,
                "fetched_at": at,
            }
        )
    return pd.DataFrame(records, columns=POSITION_COLUMNS)


def __strip_agent_suffix(member: str) -> str:
    """Remove CFFEX client-trading marker from member names.

    The exchange tags brokerage rows whose trades are made on behalf of
    clients with a "(代客)" / "（代客）" suffix. We drop it so that the same
    broker appears once in dashboards/reports.
    """
    if not member:
        return member
    for suffix in ("(代客)", "（代客）"):
        if member.endswith(suffix):
            return member[: -len(suffix)].strip()
    return member


def strip_agent_suffix(member: str) -> str:
    """Public wrapper for __strip_agent_suffix (used in tests/adapters)."""
    return __strip_agent_suffix(member)


def build_cffex_settlement_frame(market: "pd.DataFrame", source_url: str = "") -> "pd.DataFrame":
    """Build a settlement-params frame for CFFEX using default exchange rates.

    Because CFFEX rtj does not publish margin rates, we synthesize one row per
    (trade_date, contract) using CFFEX_DEFAULT_MARGIN_RATES. The result can be
    passed to PositionsDatabase.upsert_market_day alongside the market frame so
    that the contract_market_value view yields non-zero margin estimates.

    Trade-fee / close-today-fee are left as None (CFFEX publishes these as a
    fixed amount per contract, not as a ratio, and the dashboard does not
    display them).
    """
    at = fetched_at()
    records = []
    if market is None or market.empty:
        return pd.DataFrame(columns=SETTLEMENT_COLUMNS)
    for _, r in market.iterrows():
        product_code = str(r.get("product_code") or "").upper()
        rates = CFFEX_DEFAULT_MARGIN_RATES.get(product_code)
        if not rates:
            continue
        records.append(
            {
                "trade_date": r.get("trade_date"),
                "exchange": "CFFEX",
                "product_code": product_code,
                "contract": r.get("contract"),
                "settlement_price": r.get("settlement_price"),
                "spec_long_margin_rate": rates["spec_long"],
                "spec_short_margin_rate": rates["spec_short"],
                "hedge_long_margin_rate": rates["hedge_long"],
                "hedge_short_margin_rate": rates["hedge_short"],
                "trade_fee_ratio": None,
                "close_today_fee_ratio": None,
                "source_url": source_url,
                "fetched_at": at,
            }
        )
    return pd.DataFrame(records, columns=SETTLEMENT_COLUMNS)


def fetch_cffex_daily_market(
    trade_date: date,
    http: requests.Session,
) -> tuple[pd.DataFrame, str]:
    """Fetch CFFEX rtj daily statistics. Returns (market_df, source_url).

    CFFEX does not publish margin/fee params in the rtj feed, so settlement
    is empty (consistent with how the schema already accepts NULLs).
    """
    ym = trade_date.strftime("%Y%m")
    day = trade_date.strftime("%d")
    url = CFFEX_RTJ_URL.format(ym=ym, day=day)
    text = get_text(http, url, encoding="utf-8")
    market = parse_cffex_rtj_xml(text, trade_date, url)
    return market, url


def parse_czce_daily_xlsx(raw: pd.DataFrame, trade_date: date, source_url: str) -> pd.DataFrame:
    """Parse a CZCE FutureDataDaily.xlsx into the MARKET_COLUMNS schema.

    The xlsx has a title row (row 0) then a header row at row 1 with columns:
    合约代码 / 昨结算 / 今开盘 / 最高价 / 最低价 / 今收盘 / 今结算 / 涨跌1 /
    涨跌2 / 成交量(手) / 持仓量 / 增减量 / 成交额(万元). Numeric cells are
    comma-formatted strings so we strip commas. Rows whose 合约代码 is not a
    real instrument (小计/合计 totals, or non-\d-suffixed codes) are dropped.
    """
    at = fetched_at()
    # find the header row (contains 合约代码)
    header_idx = None
    for i in range(min(8, len(raw))):
        cells = [str(v) for v in raw.iloc[i].tolist()]
        if any("合约代码" in c for c in cells):
            header_idx = i
            break
    if header_idx is None:
        return pd.DataFrame(columns=MARKET_COLUMNS)
    df = raw.iloc[header_idx + 1:].copy()
    df.columns = [str(v).strip() for v in raw.iloc[header_idx].tolist()]
    df = df.reset_index(drop=True)

    records: list[dict] = []
    for _, row in df.iterrows():
        contract = str(row.get("合约代码") or "").strip()
        if not re.fullmatch(r"[A-Za-z]{1,3}\d{3,4}", contract):
            continue
        product_code = re.sub(r"\d+$", "", contract)
        product_name = CZCE_SPECS.get(product_code, (product_code, 0, ""))[0]
        def num(col):
            v = row.get(col)
            if v is None or v == "" or (isinstance(v, float) and v != v):
                return None
            try:
                return float(str(v).replace(",", ""))
            except (TypeError, ValueError):
                return None
        records.append(
            {
                "trade_date": trade_date.isoformat(),
                "exchange": "CZCE",
                "product_code": product_code,
                "product_name": product_name,
                "contract": contract,
                "open_price": num("今开盘"),
                "high_price": num("最高价"),
                "low_price": num("最低价"),
                "close_price": num("今收盘"),
                "settlement_price": num("今结算"),
                "pre_settlement_price": num("昨结算"),
                "volume": num("成交量(手)"),
                "open_interest": num("持仓量"),
                "open_interest_change": num("增减量"),
                "turnover": num("成交额(万元)"),
                "source_url": source_url,
                "fetched_at": at,
            }
        )
    return pd.DataFrame(records, columns=MARKET_COLUMNS)


def fetch_czce_daily_market(
    trade_date: date,
    http: requests.Session,
) -> tuple[pd.DataFrame, str]:
    """Fetch CZCE daily market (FutureDataDaily.xlsx) from the official site.

    Returns (market_df, source_url). CZCE must be fetched over http (https
    returns 412) and the .xlsx extension (not .htm/.txt).
    """
    from io import BytesIO

    d = yyyymmdd(trade_date)
    candidates = [
        f"http://www.czce.com.cn/cn/DFSStaticFiles/Future/{trade_date.year}/{d}/FutureDataDaily.xlsx",
        f"http://www.czce.com.cn/cn/DFSStaticFiles/Future/{trade_date.year}/{d}/FutureDataDaily.xls",
    ]
    last_error: Exception | None = None
    for url in candidates:
        try:
            resp = http.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            resp.raise_for_status()
            raw = pd.read_excel(BytesIO(resp.content), header=None)
            market = parse_czce_daily_xlsx(raw, trade_date, url)
            return market, url
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"CZCE market fetch failed: {last_error}")


def parse_czce_holding_xlsx(raw: pd.DataFrame, trade_date: date, source_url: str) -> pd.DataFrame:
    """Parse a CZCE FutureDataHolding.xlsx into a normalized positions frame.

    CZCE's holding layout differs from the generic Chinese rank-table shape:
    each product is announced by a title row '品种：苹果AP 日期：...', followed
    by a header row and 20 rank rows whose 10 cells are
    [rank, vol_member, vol, vol_chg, long_member, long_oi, long_chg,
     short_member, short_oi, short_chg] (i.e. three members per rank).
    Returns a DataFrame matching the positions upsert schema.
    """
    at = fetched_at()
    records: list[dict] = []
    if raw is None or raw.empty:
        return pd.DataFrame()
    current_product = ""
    for _, row in raw.iterrows():
        cells = row.tolist()
        first = "" if (cells[0] != cells[0]) else str(cells[0]).strip()  # NaN-safe
        # product title row: '品种：苹果AP 日期：...'
        if first.startswith("品种") or first.startswith("合约"):
            # CZCE品种 row: "品种：苹果AP     日期：2026-06-05" — the product
            # code is the uppercase letters immediately before whitespace + 日期.
            m = re.search(r"([A-Za-z]{2,4})\s+日期", first)
            if m:
                current_product = m.group(1)
            continue
        # header row (名次...)
        if first == "名次" or "会员简称" in " ".join(str(c) for c in cells):
            continue
        rank = clean_number(first)
        if rank is None or not (1 <= int(rank) <= 20):
            continue
        # collect the 9 value cells (skip rank at index 0)
        def cell(i):
            v = cells[i] if i < len(cells) else None
            if v != v:  # NaN
                return None
            return v
        # triplet layout: (member, value, change) for volume/long/short
        triplets = [
            ("volume", cell(1), cell(2), cell(3)),
            ("long",   cell(4), cell(5), cell(6)),
            ("short",  cell(7), cell(8), cell(9)),
        ]
        for metric, member_raw, value_raw, change_raw in triplets:
            member = "" if member_raw is None else str(member_raw).strip()
            # strip trailing （代客） agency suffix
            member = re.sub(r"[（(]代客[）)]$", "", member).strip()
            value = clean_number(value_raw)
            change = clean_number(change_raw)
            if not member and value is None:
                continue
            records.append({
                "trade_date": trade_date.isoformat(),
                "exchange": "CZCE",
                "product": current_product,
                "contract": current_product,
                "rank": int(rank),
                "metric": metric,
                "member": member,
                "value": value,
                "change": change,
                "source_url": source_url,
                "fetched_at": at,
            })
    return pd.DataFrame(records)


def build_czce_settlement_frame(market: pd.DataFrame, source_url: str = "") -> pd.DataFrame:
    """Build a settlement-params frame for CZCE using default exchange margins.

    CZCE does not publish per-day margin rates in the daily xlsx, so we
    synthesize one row per (trade_date, contract) from
    CZCE_DEFAULT_MARGIN_RATES (spec == hedge per the exchange baseline).
    Trade-fee / close-today-fee are left None.
    """
    at = fetched_at()
    records: list[dict] = []
    if market is None or market.empty:
        return pd.DataFrame(columns=SETTLEMENT_COLUMNS)
    for _, r in market.iterrows():
        product_code = str(r.get("product_code") or "").upper()
        rate = CZCE_DEFAULT_MARGIN_RATES.get(product_code)
        if rate is None:
            continue
        records.append(
            {
                "trade_date": r.get("trade_date"),
                "exchange": "CZCE",
                "product_code": product_code,
                "contract": r.get("contract"),
                "settlement_price": r.get("settlement_price"),
                "spec_long_margin_rate": rate,
                "spec_short_margin_rate": rate,
                "hedge_long_margin_rate": rate,
                "hedge_short_margin_rate": rate,
                "trade_fee_ratio": None,
                "close_today_fee_ratio": None,
                "source_url": source_url,
                "fetched_at": at,
            }
        )
    return pd.DataFrame(records, columns=SETTLEMENT_COLUMNS)
