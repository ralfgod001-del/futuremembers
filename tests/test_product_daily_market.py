"""Tests for the new productDailyMarket aggregation."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from futures_positions.database import PositionsDatabase


def seed_market(db_path: Path) -> None:
    """Bootstrap a minimal SHFE database with market + specs data."""
    PositionsDatabase(db_path).initialize()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = [
        ("2026-06-01", "SHFE", "cu", "铜", "cu2606", 70000, 71000, 69000, 70500, 70000, 69000, 100, 1000, 0, 7000000, "t", "t"),
        ("2026-06-01", "SHFE", "cu", "铜", "cu2607", 71000, 72000, 70000, 71500, 71000, 70000, 100, 1000, 0, 7100000, "t", "t"),
        ("2026-06-02", "SHFE", "cu", "铜", "cu2606", 71500, 72500, 70500, 72000, 72000, 70000, 110, 1100, 100, 7200000, "t", "t"),
        ("2026-06-02", "SHFE", "cu", "铜", "cu2607", 72500, 73500, 71500, 73000, 73000, 71000, 110, 1100, 100, 7300000, "t", "t"),
        ("2026-06-01", "SHFE", "al", "铝", "al2606", 20000, 20500, 19500, 20000, 20000, 19500, 50, 500, 0, 1000000, "t", "t"),
        ("2026-06-02", "SHFE", "al", "铝", "al2606", 20200, 20700, 19700, 20200, 20200, 20000, 60, 600, 100, 1010000, "t", "t"),
    ]
    cur.executemany("""
        INSERT INTO contract_daily_market
        (trade_date, exchange, product_code, product_name, contract,
         open_price, high_price, low_price, close_price,
         settlement_price, pre_settlement_price,
         volume, open_interest, open_interest_change, turnover, source_url, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    cur.executemany(
        "INSERT OR REPLACE INTO market_sync_status (trade_date, status, last_attempt_at) VALUES (?, 'ok', ?)",
        [("2026-06-01", "2026-06-01T00:00:00"), ("2026-06-02", "2026-06-02T00:00:00")],
    )
    # Seed one positions row per (date, product) so dashboard_payload doesn't short-circuit.
    pos_rows = []
    for trade_date in ("2026-06-01", "2026-06-02"):
        for product, contract in [("铜", "cu2606"), ("铝", "al2606")]:
            for metric, value in (("volume", 100), ("long", 50), ("short", 40)):
                pos_rows.append((
                    trade_date, "SHFE", product, contract, 1, metric,
                    "某会员", value, 0, "t", "2026-01-01T00:00:00+08:00",
                ))
    cur.executemany("""
        INSERT OR REPLACE INTO positions
        (trade_date, exchange, product, contract, rank, metric,
         member, value, change, source_url, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, pos_rows)
    cur.executemany(
        "INSERT OR REPLACE INTO sync_status (trade_date, status, last_attempt_at) VALUES (?, 'ok', ?)",
        [("2026-06-01", "2026-06-01T00:00:00"), ("2026-06-02", "2026-06-02T00:00:00")],
    )
    conn.commit()
    conn.close()


class ProductDailyMarketTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.db_path = Path(self._tmp) / "t.sqlite"
        seed_market(self.db_path)

    def tearDown(self):
        # Best-effort cleanup; ignore Windows file locks from pandas/sqlite cache.
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass

    def test_product_daily_market_is_oi_weighted(self):
        payload = PositionsDatabase(self.db_path).dashboard_payload()
        pdm = payload["productDailyMarket"]
        self.assertGreater(len(pdm), 0)
        cu_jun1 = next(r for r in pdm if r["trade_date"] == "2026-06-01" and r["product"] == "铜")
        # 1000 OI @ 70000 + 1000 OI @ 71000 -> 70500 weighted
        self.assertAlmostEqual(cu_jun1["settlement_price"], 70500.0, places=2)
        self.assertEqual(cu_jun1["open_interest"], 2000.0)
        cu_jun2 = next(r for r in pdm if r["trade_date"] == "2026-06-02" and r["product"] == "铜")
        self.assertAlmostEqual(cu_jun2["settlement_price"], 72500.0, places=2)
        al_jun1 = next(r for r in pdm if r["trade_date"] == "2026-06-01" and r["product"] == "铝")
        self.assertAlmostEqual(al_jun1["settlement_price"], 20000.0, places=2)

    def test_product_daily_market_keys(self):
        payload = PositionsDatabase(self.db_path).dashboard_payload()
        self.assertIn("productDailyMarket", payload)
        sample = payload["productDailyMarket"][0]
        for key in ("trade_date", "product", "settlement_price", "open_interest"):
            self.assertIn(key, sample)


if __name__ == "__main__":
    unittest.main()
