from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from futures_positions.adapters import SHFEAdapter, ranked_shfe_rows
from futures_positions.database import PositionsDatabase
from futures_positions.market_data import parse_daily_market, parse_settlement_params
from futures_positions.models import ExchangeData
from futures_positions.system import update_incremental, update_market_incremental


def position_rows(trade_date: str, member: str, long_value: int, short_value: int) -> pd.DataFrame:
    base = {
        "trade_date": trade_date,
        "exchange": "SHFE",
        "product": "铜",
        "contract": f"cu{trade_date[2:4]}01",
        "rank": 1,
        "member": member,
        "change": 0,
        "source_url": "test",
        "fetched_at": "2026-01-01T00:00:00+08:00",
    }
    return pd.DataFrame(
        [
            {**base, "metric": "long", "value": long_value},
            {**base, "metric": "short", "value": short_value},
            {**base, "metric": "volume", "value": long_value + short_value},
        ]
    )


class FakeAdapter(SHFEAdapter):
    def __init__(self):
        self.called: list[date] = []

    def fetch_official(self, trade_date, http):
        self.called.append(trade_date)
        frame = position_rows(trade_date.isoformat(), "测试会员", 10, 8)
        return ExchangeData("SHFE", frame)


class PositionsSystemTest(unittest.TestCase):
    def test_shfe_rank_filter_keeps_only_rank_1_to_20(self):
        payload = {
            "o_cursor": [
                {"RANK": -1},
                {"RANK": 1},
                {"RANK": 20},
                {"RANK": 21},
                {"RANK": 999},
            ]
        }
        self.assertEqual([row["RANK"] for row in ranked_shfe_rows(payload)], [1, 20])

    def test_database_upsert_filters_invalid_rank_and_blank_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = PositionsDatabase(Path(tmp) / "positions.sqlite")
            valid = position_rows("2026-01-05", "测试会员", 10, 8)
            invalid_rank = valid.copy()
            invalid_rank["rank"] = 999
            blank_member = valid.copy()
            blank_member["member"] = ""
            inserted = database.upsert_frame(pd.concat([valid, invalid_rank, blank_member]))
            self.assertEqual(inserted, 3)
            self.assertEqual(database.status()["row_count"], 3)

    def test_incremental_update_only_downloads_missing_weekday(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = PositionsDatabase(Path(tmp) / "positions.sqlite")
            database.upsert_frame(position_rows("2026-01-05", "已有会员", 5, 4))
            adapter = FakeAdapter()
            result = update_incremental(
                database,
                date(2026, 1, 5),
                date(2026, 1, 6),
                pause_seconds=0,
                adapter=adapter,
                http=object(),
                trading_days={"20260105", "20260106"},
            )
            self.assertEqual(adapter.called, [date(2026, 1, 6)])
            self.assertEqual(result["downloaded"], 1)
            self.assertEqual(database.status()["trading_days"], 2)

    def test_missing_member_date_is_zero_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = PositionsDatabase(Path(tmp) / "positions.sqlite")
            database.upsert_frame(position_rows("2026-01-05", "会员A", 10, 8))
            database.upsert_frame(position_rows("2026-01-06", "会员B", 20, 15))
            database.upsert_frame(position_rows("2026-01-07", "会员A", 30, 12))

            series = database.member_series("会员A", product="铜", metric="long")
            self.assertEqual(
                series,
                [
                    {"trade_date": "2026-01-05", "value": 10.0},
                    {"trade_date": "2026-01-06", "value": 0},
                    {"trade_date": "2026-01-07", "value": 30.0},
                ],
            )

    def test_no_data_date_stops_retrying_after_three_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = PositionsDatabase(Path(tmp) / "positions.sqlite")
            database.initialize()
            for _ in range(3):
                database.mark_sync("2026-01-05", "no_data")
            self.assertNotIn(
                date(2026, 1, 5),
                database.missing_weekdays(date(2026, 1, 5), date(2026, 1, 5)),
            )

    def test_missing_dates_respect_trading_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = PositionsDatabase(Path(tmp) / "positions.sqlite")
            database.initialize()
            missing = database.missing_weekdays(
                date(2026, 1, 5),
                date(2026, 1, 7),
                trading_days={"20260105", "20260107"},
            )
            self.assertEqual(missing, [date(2026, 1, 5), date(2026, 1, 7)])

    def test_daily_market_parser_and_notional_value_calculation(self):
        trade_date = date(2026, 1, 5)
        market = parse_daily_market(
            {
                "o_curinstrument": [
                    {
                        "PRODUCTGROUPID": "cu",
                        "DELIVERYMONTH": "2602",
                        "SETTLEMENTPRICE": 100,
                        "OPENINTEREST": 10,
                        "VOLUME": 8,
                    },
                    {"PRODUCTGROUPID": "cu", "DELIVERYMONTH": "小计"},
                ]
            },
            trade_date,
            "market-url",
        )
        settlement = parse_settlement_params(
            {
                "o_cursor": [
                    {
                        "INSTRUMENTID": "cu2602",
                        "SETTLEMENTPRICE": 100,
                        "SPECLONGMARGINRATIO": 0.1,
                        "SPECSHORTMARGINRATIO": 0.12,
                        "HEDGLONGMARGINRATIO": 0.08,
                        "HEDGSHORTMARGINRATIO": 0.09,
                    }
                ]
            },
            trade_date,
            "settlement-url",
        )

        with tempfile.TemporaryDirectory() as tmp:
            database = PositionsDatabase(Path(tmp) / "positions.sqlite")
            counts = database.upsert_market_day(market, settlement)
            with database.session() as connection:
                value = connection.execute(
                    """
                    SELECT notional_value, estimated_spec_margin, estimated_hedge_margin
                    FROM contract_market_value
                    """
                ).fetchone()
            self.assertEqual(counts, {"market_rows": 1, "settlement_rows": 1})
            self.assertEqual(value["notional_value"], 5000)
            self.assertAlmostEqual(value["estimated_spec_margin"], 1100)
            self.assertAlmostEqual(value["estimated_hedge_margin"], 850)

    def test_market_incremental_only_downloads_missing_day(self):
        trade_date = date(2026, 1, 5)
        market = parse_daily_market(
            {
                "o_curinstrument": [
                    {
                        "PRODUCTGROUPID": "cu",
                        "DELIVERYMONTH": "2602",
                        "SETTLEMENTPRICE": 100,
                        "OPENINTEREST": 10,
                    }
                ]
            },
            trade_date,
            "market-url",
        )
        settlement = parse_settlement_params(
            {"o_cursor": [{"INSTRUMENTID": "cu2602", "SETTLEMENTPRICE": 100}]},
            trade_date,
            "settlement-url",
        )
        called = []

        def fetcher(day, http):
            called.append(day)
            day_market = market.copy()
            day_market["trade_date"] = day.isoformat()
            day_settlement = settlement.copy()
            day_settlement["trade_date"] = day.isoformat()
            return day_market, day_settlement, "market-url", "settlement-url"

        with tempfile.TemporaryDirectory() as tmp:
            database = PositionsDatabase(Path(tmp) / "positions.sqlite")
            database.upsert_market_day(market, settlement)
            result = update_market_incremental(
                database,
                date(2026, 1, 5),
                date(2026, 1, 6),
                pause_seconds=0,
                http=object(),
                trading_days={"20260105", "20260106"},
                fetcher=fetcher,
            )
            self.assertEqual(called, [date(2026, 1, 6)])
            self.assertEqual(result["downloaded"], 1)
            self.assertEqual(database.status()["market"]["trading_days"], 2)


if __name__ == "__main__":
    unittest.main()
