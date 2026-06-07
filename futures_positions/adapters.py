from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from io import BytesIO, StringIO
from xml.etree import ElementTree

import pandas as pd
import requests

from .models import ExchangeData
from .utils import clean_number, clean_text, fetched_at, flatten_columns, get_text, yyyymmdd


METRIC_MAP = {
    "volume": ("PARTICIPANTABBR1", "CJ1", "CJ1_CHG"),
    "long": ("PARTICIPANTABBR2", "CJ2", "CJ2_CHG"),
    "short": ("PARTICIPANTABBR3", "CJ3", "CJ3_CHG"),
}


logger = logging.getLogger(__name__)


def ranked_shfe_rows(payload: dict) -> list[dict]:
    rows = payload.get("o_cursor") or payload.get("o_curinstrument") or []
    return [
        row
        for row in rows
        if clean_number(row.get("RANK")) and 0 < clean_number(row.get("RANK")) <= 20
    ]

CFFEX_PRODUCTS = ["IF", "IC", "IM", "IH", "TS", "TF", "T", "TL", "IO", "MO", "HO"]

class BaseAdapter:
    exchange = ""

    def fetch(self, trade_date: date, s: requests.Session) -> ExchangeData:
        raise NotImplementedError

    def _records_from_rank_rows(self, trade_date: date, rows, source_url: str) -> list[dict]:
        records = []
        at = fetched_at()
        for row in rows:
            normalized = {str(k).upper(): v for k, v in dict(row).items()}
            product = clean_text(
                normalized.get("PRODUCTNAME")
                or normalized.get("PRODUCT")
                or normalized.get("品种")
                or normalized.get("合约")
            )
            contract = clean_text(
                normalized.get("INSTRUMENTID")
                or normalized.get("INSTRUMENT")
                or normalized.get("CONTRACT")
                or normalized.get("合约")
                or normalized.get("合约代码")
            )
            rank = clean_number(normalized.get("RANK") or normalized.get("名次"))
            for metric, (member_key, value_key, change_key) in METRIC_MAP.items():
                member = clean_text(normalized.get(member_key))
                value = clean_number(normalized.get(value_key))
                change = clean_number(normalized.get(change_key))
                if not member or value is None:
                    continue
                records.append(
                    {
                        "trade_date": trade_date.isoformat(),
                        "exchange": self.exchange,
                        "product": product,
                        "contract": contract,
                        "rank": rank,
                        "metric": metric,
                        "member": member,
                        "value": value,
                        "change": change,
                        "source_url": source_url,
                        "fetched_at": at,
                    }
                )
        return records


class SHFEAdapter(BaseAdapter):
    exchange = "SHFE"
    base_url = "https://www.shfe.com.cn/data/tradedata/future/dailydata/pm{date}.dat"

    def fetch(self, trade_date: date, s: requests.Session) -> ExchangeData:
        # Official exchange endpoint is the ONLY source for member positions.
        # No third-party mirror (e.g. akshare) is used so the data lineage is
        # always the exchange itself; failures propagate to the caller.
        return self.fetch_official(trade_date, s)

    def fetch_official(self, trade_date: date, s: requests.Session) -> ExchangeData:
        url = self.base_url.format(date=yyyymmdd(trade_date))
        text = get_text(s, url, encoding="utf-8")
        payload = json.loads(text)
        rows = ranked_shfe_rows(payload)
        records = self._records_from_rank_rows(trade_date, rows, url)
        raw = pd.DataFrame(rows)
        return ExchangeData(self.exchange, pd.DataFrame(records), {"raw": raw}, [url])


class INEAdapter(SHFEAdapter):
    exchange = "INE"
    base_url = "https://www.ine.cn/data/dailydata/kx/pm{date}.dat"

    # Inherits SHFEAdapter.fetch / fetch_official, which pull the .dat JSON
    # straight from the INE official endpoint. No third-party mirror.


class DCEAdapter(BaseAdapter):
    exchange = "DCE"

    def fetch(self, trade_date: date, s: requests.Session) -> ExchangeData:
        # Official DCE export endpoint is the ONLY source for member positions.
        url = (
            "http://www.dce.com.cn/publicweb/quotesdata/exportMemberDealPosiQuotesBatchData.html"
            f"?memberDealPosiQuotes.variety=all&memberDealPosiQuotes.trade_type=0"
            f"&year={trade_date.year}&month={trade_date.month - 1}&day={trade_date.day}&exportFlag=txt"
        )
        text = get_text(s, url, encoding="gbk")
        raw = self._read_text_table(text)
        records = self._normalize_wide_table(trade_date, raw, url)
        return ExchangeData(self.exchange, pd.DataFrame(records), {"raw": raw}, [url])

    def _read_text_table(self, text: str) -> pd.DataFrame:
        lines = [line for line in text.splitlines() if line.strip()]
        table_text = "\n".join(lines)
        try:
            return pd.read_csv(StringIO(table_text), sep=r"\s+|\t|,", engine="python")
        except Exception:
            return pd.DataFrame({"raw": lines})

    def _normalize_wide_table(self, trade_date: date, df: pd.DataFrame, source_url: str) -> list[dict]:
        return normalize_chinese_rank_table(self.exchange, trade_date, df, source_url)


class CZCEAdapter(BaseAdapter):
    exchange = "CZCE"

    def fetch(self, trade_date: date, s: requests.Session) -> ExchangeData:
        # Official CZCE position-rank file is the ONLY source.
        # CZCE publishes FutureDataHolding.xlsx per trading day under
        # http://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{YYYYMMDD}/.
        # NOTE: must use http (https returns 412) and the .xlsx extension
        # (not .htm/.txt, which 404). The xlsx uses an embedded header row
        # (名次/会员简称/交易量/.../持买仓量/.../持卖仓量/...) so we parse it
        # directly and feed the flat rows to the shared Chinese-table normalizer.
        d = yyyymmdd(trade_date)
        # 2025-11-01 onward CZCE switched .xls -> .xlsx; try both for safety.
        candidates = [
            f"http://www.czce.com.cn/cn/DFSStaticFiles/Future/{trade_date.year}/{d}/FutureDataHolding.xlsx",
            f"http://www.czce.com.cn/cn/DFSStaticFiles/Future/{trade_date.year}/{d}/FutureDataHolding.xls",
        ]
        last_error: Exception | None = None
        for url in candidates:
            try:
                resp = s.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                resp.raise_for_status()
                raw = pd.read_excel(BytesIO(resp.content), header=None)
                records = normalize_chinese_rank_table(self.exchange, trade_date, raw, url)
                return ExchangeData(self.exchange, pd.DataFrame(records), {"raw": raw}, [url])
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"CZCE 数据下载失败: {last_error}")


class CFFEXAdapter(BaseAdapter):
    """CFFEX position-rank adapter.

    Pulls per-product XML from /sj/ccpm/{ym}/{day}/{product}.xml and parses
    each <data> row directly (no akshare fallback for positions). The XML
    publishes datatypes 0/1/2 = volume/long/short for ranks 1..20.
    """

    exchange = "CFFEX"
    base_url = "http://www.cffex.com.cn/sj/ccpm/{ym}/{day}/{product}.xml"

    def fetch(self, trade_date: date, s: requests.Session) -> ExchangeData:
        from .market_data import parse_cffex_position_xml  # avoid cycle

        all_records = []
        raw_tables: dict[str, pd.DataFrame] = {}
        source_urls: list[str] = []
        ym = trade_date.strftime("%Y%m")
        day = trade_date.strftime("%d")
        for product in CFFEX_PRODUCTS:
            if product not in {"IF", "IC", "IM", "IH", "TS", "TF", "T", "TL"}:
                # Skip equity-index options for the positions adapter (they
                # use a different instrument scheme and aren't in CFFEX_SPECS).
                continue
            url = self.base_url.format(ym=ym, day=day, product=product)
            try:
                text = get_text(s, url, encoding="utf-8")
            except Exception as exc:
                logger.warning(
                    "CFFEX %s positions download failed for %s (%s)",
                    product, trade_date.isoformat(), exc,
                )
                continue
            try:
                frame = parse_cffex_position_xml(text, trade_date, product, url)
            except Exception as exc:
                # XML parse failed: try the generic HTML-table normalizer as a
                # last resort for older CFFEX layouts so we never lose a day.
                logger.debug(
                    "CFFEX %s positions XML parse failed for %s, trying HTML fallback (%s)",
                    product, trade_date.isoformat(), exc,
                )
                stripped = text.strip()
                if stripped.startswith("<"):
                    continue
                tables = pd.read_html(StringIO(stripped))
                raw = pd.concat([flatten_columns(t) for t in tables], ignore_index=True)
                raw["product"] = product
                records = normalize_chinese_rank_table(self.exchange, trade_date, raw, url, default_product=product)
                frame = pd.DataFrame(records)
            if not frame.empty:
                all_records.append(frame)
                raw_tables[product] = frame
                source_urls.append(url)
            # Polite rate-limit: ≥1.2s between CFFEX requests so we don't
            # hammer a single IP within a single day's 8-product scan.
            time.sleep(1.2)
        if all_records:
            combined = pd.concat(all_records, ignore_index=True)
        else:
            combined = pd.DataFrame(columns=[
                "trade_date", "exchange", "product", "contract", "rank",
                "metric", "member", "value", "change", "source_url", "fetched_at",
            ])
        return ExchangeData(self.exchange, combined, raw_tables, source_urls)


def normalize_chinese_rank_table(
    exchange: str,
    trade_date: date,
    df: pd.DataFrame,
    source_url: str,
    default_product: str = "",
) -> list[dict]:
    if df.empty:
        return []
    df = flatten_columns(df)
    records = []
    at = fetched_at()
    current_product = default_product
    current_contract = ""

    for _, row in df.iterrows():
        values = [clean_text(v) for v in row.tolist()]
        joined = " ".join(values)
        if not joined:
            continue
        header_like = any(key in joined for key in ["名次", "会员简称", "成交量", "持买单", "持卖单"])
        if header_like:
            continue

        first = values[0] if values else ""
        if first and clean_number(first) is None and len([v for v in values if v]) <= 3:
            current_product = re.sub(r"品种|合约|：|:", "", first).strip() or current_product
            current_contract = current_product
            continue

        rank = first if clean_number(first) is not None else find_first_number(values)
        if clean_number(rank) is None:
            possible_contract = find_contract(values)
            if possible_contract:
                current_contract = possible_contract
                if not current_product:
                    current_product = re.sub(r"\d+$", "", possible_contract)
            continue

        non_empty = [v for v in values if v]
        if len(non_empty) < 4:
            continue

        # Most exchange ranking tables repeat triplets:
        # rank, member(volume), volume, change, member(long), long, change, member(short), short, change.
        tail = non_empty[1:]
        triplets = [tail[0:3], tail[3:6], tail[6:9]]
        for metric, triplet in zip(["volume", "long", "short"], triplets):
            if len(triplet) < 2:
                continue
            member = triplet[0]
            value = clean_number(triplet[1])
            change = clean_number(triplet[2]) if len(triplet) > 2 else None
            if not member and value is None:
                continue
            records.append(
                {
                    "trade_date": trade_date.isoformat(),
                    "exchange": exchange,
                    "product": current_product,
                    "contract": current_contract or current_product,
                    "rank": clean_number(rank),
                    "metric": metric,
                    "member": member,
                    "value": value,
                    "change": change,
                    "source_url": source_url,
                    "fetched_at": at,
                }
            )
    return records


def find_first_number(values: list[str]) -> str:
    for value in values:
        if clean_number(value) is not None:
            return value
    return ""


def find_contract(values: list[str]) -> str:
    for value in values:
        if re.fullmatch(r"[A-Za-z]{1,3}\d{3,4}|[A-Za-z]{1,3}", value):
            return value
    return ""
