"""Tests for the CFFEX integration (specs, rtj parser, position parser)."""
from __future__ import annotations

import unittest
from datetime import date
from xml.etree import ElementTree

import pandas as pd

from futures_positions.adapters import CFFEXAdapter
from futures_positions.market_data import (
    CFFEX_FUTURE_PRODUCTS,
    CFFEX_SPECS,
    contract_spec_rows,
    exchange_for_product,
    fetch_cffex_daily_market,
    parse_cffex_position_xml,
    parse_cffex_rtj_xml,
    strip_agent_suffix,
)


RTJ_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<root>
  <dailydata>
    <instrumentid>IF2606</instrumentid>
    <productid>IF</productid>
    <openprice>4800</openprice>
    <highestprice>4900</highestprice>
    <lowestprice>4780</lowestprice>
    <closeprice>4870</closeprice>
    <settlementprice>4870.6</settlementprice>
    <presettlementprice>4820</presettlementprice>
    <volume>68628</volume>
    <openinterest>117531</openinterest>
    <turnover>1234567</turnover>
  </dailydata>
  <dailydata>
    <instrumentid>IF2506-C-4200</instrumentid>
    <productid>IF</productid>
    <settlementprice>200</settlementprice>
  </dailydata>
  <dailydata>
    <instrumentid>T2606</instrumentid>
    <productid>T</productid>
    <settlementprice>101.5</settlementprice>
    <volume>50000</volume>
    <openinterest>120000</openinterest>
  </dailydata>
</root>
"""


POSITION_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<root>
  <data>
    <instrumentid>IF2606</instrumentid>
    <tradingday>20260604</tradingday>
    <datatypeid>1</datatypeid>
    <rank>1</rank>
    <shortname>国泰君安(代客)</shortname>
    <volume>16905</volume>
    <varvolume>-2386</varvolume>
    <productid>IF</productid>
  </data>
  <data>
    <instrumentid>IF2606</instrumentid>
    <tradingday>20260604</tradingday>
    <datatypeid>2</datatypeid>
    <rank>1</rank>
    <shortname>中信期货（代客）</shortname>
    <volume>18000</volume>
    <varvolume>500</varvolume>
    <productid>IF</productid>
  </data>
  <data>
    <instrumentid>IF2606</instrumentid>
    <tradingday>20260604</tradingday>
    <datatypeid>0</datatypeid>
    <rank>21</rank>
    <shortname>跳过</shortname>
    <volume>10</volume>
  </data>
</root>
"""


class CFFEXSpecsTest(unittest.TestCase):
    def test_cffex_specs_cover_eight_futures(self):
        self.assertEqual(
            sorted(CFFEX_SPECS.keys()),
            ["IC", "IF", "IH", "IM", "T", "TF", "TL", "TS"],
        )

    def test_exchange_for_product_uppercase_cffex(self):
        self.assertEqual(exchange_for_product("IF"), "CFFEX")
        self.assertEqual(exchange_for_product("T"), "CFFEX")
        self.assertEqual(exchange_for_product("cu"), "SHFE")
        self.assertEqual(exchange_for_product("sc"), "INE")

    def test_contract_spec_rows_include_cffex(self):
        rows = contract_spec_rows()
        cffex = [r for r in rows if r["exchange"] == "CFFEX"]
        self.assertEqual(len(cffex), 8)
        names = {r["product_code"]: r["product_name"] for r in cffex}
        self.assertEqual(names["IF"], "沪深300")
        self.assertEqual(names["TL"], "30年期国债")
        # Multiplier sanity checks
        mult = {r["product_code"]: r["contract_multiplier"] for r in cffex}
        self.assertEqual(mult["IF"], 200)
        self.assertEqual(mult["IH"], 300)
        self.assertEqual(mult["TS"], 20000)


class CFFEXRtjParserTest(unittest.TestCase):
    def test_parses_futures_and_drops_options(self):
        df = parse_cffex_rtj_xml(RTJ_SAMPLE, date(2026, 6, 4), "https://example/rtj")
        # Two futures rows; the option IF2506-C-4200 must be filtered out.
        self.assertEqual(len(df), 2)
        self.assertEqual(set(df["product_code"]), {"IF", "T"})
        self.assertEqual(set(df["exchange"]), {"CFFEX"})
        if_row = df[df["contract"] == "IF2606"].iloc[0]
        self.assertAlmostEqual(if_row["settlement_price"], 4870.6)
        self.assertEqual(if_row["volume"], 68628)
        self.assertEqual(if_row["open_interest"], 117531)
        # product_name comes from CFFEX_SPECS
        self.assertEqual(if_row["product_name"], "沪深300")

    def test_empty_xml_returns_empty_frame_with_columns(self):
        df = parse_cffex_rtj_xml(
            "<root></root>",
            date(2026, 6, 4),
            "https://example/empty",
        )
        self.assertEqual(len(df), 0)
        self.assertIn("settlement_price", df.columns)


class CFFEXPositionParserTest(unittest.TestCase):
    def test_strip_agent_suffix_handles_halfwidth_and_fullwidth(self):
        self.assertEqual(strip_agent_suffix("国泰君安(代客)"), "国泰君安")
        self.assertEqual(strip_agent_suffix("中信期货（代客）"), "中信期货")
        self.assertEqual(strip_agent_suffix("永安期货"), "永安期货")
        self.assertEqual(strip_agent_suffix(""), "")

    def test_parse_position_xml_strips_agent_and_filters_rank(self):
        df = parse_cffex_position_xml(
            POSITION_SAMPLE,
            date(2026, 6, 4),
            "IF",
            "https://example/IF.xml",
        )
        # Two valid rows (rank 1 long + short); rank 21 dropped.
        self.assertEqual(len(df), 2)
        members = set(df["member"])
        self.assertEqual(members, {"国泰君安", "中信期货"})
        metrics = set(df["metric"])
        self.assertEqual(metrics, {"long", "short"})
        # Values from the long row
        long_row = df[df["metric"] == "long"].iloc[0]
        self.assertEqual(long_row["value"], 16905)
        self.assertEqual(long_row["change"], -2386)
        self.assertEqual(long_row["exchange"], "CFFEX")
        self.assertEqual(long_row["contract"], "IF2606")


class CFFEXAdapterTest(unittest.TestCase):
    def test_cffex_adapter_constants(self):
        adapter = CFFEXAdapter()
        self.assertEqual(adapter.exchange, "CFFEX")
        self.assertIn("{ym}", adapter.base_url)
        self.assertIn("{product}", adapter.base_url)


if __name__ == "__main__":
    unittest.main()
