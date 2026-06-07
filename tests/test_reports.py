"""Tests for futures_positions.reports: aggregation + DeepSeek helper."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from futures_positions.database import PositionsDatabase
from futures_positions.reports import (
    DEEPSEEK_API_URL,
    build_ai_prompt,
    call_deepseek,
    extract_text,
    top5_daily_summary,
)


def seed_positions(db_path: Path) -> None:
    """Create a tiny positions table with two products across three days.

    For each (trade_date, product), we insert 5 distinct members, each on a
    single contract, with metrics long / short / volume. One row per (member,
    metric) so the SUM(value) GROUP BY member aggregation is straightforward.
    """
    PositionsDatabase(db_path).initialize()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = []
    base_long = [100, 80, 60, 40, 20]
    base_short = [90, 70, 50, 30, 10]
    for di, trade_date in enumerate(("2026-06-01", "2026-06-02", "2026-06-03")):
        for product in ("\u94dc", "\u94dd"):
            for mi in range(5):
                member = f"\u4f1a\u5458{mi}"
                contract = f"{product}{di}{mi}"
                long_v = base_long[mi] + di * (5 + mi)
                short_v = max(0, base_short[mi] - di * (3 + mi))
                vol = long_v + short_v
                for metric, value in (("long", long_v), ("short", short_v), ("volume", vol)):
                    rows.append((trade_date, "SHFE", product, contract, mi + 1,
                                 metric, member, value, 0, "t", "2026-01-01T00:00:00+08:00"))
    cur.executemany("""
        INSERT INTO positions
        (trade_date, exchange, product, contract, rank, metric,
         member, value, change, source_url, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    cur.executemany(
        "INSERT OR REPLACE INTO sync_status (trade_date, status, last_attempt_at) VALUES (?, 'ok', ?)",
        [
            ("2026-06-01", "2026-06-01T00:00:00"),
            ("2026-06-02", "2026-06-02T00:00:00"),
            ("2026-06-03", "2026-06-03T00:00:00"),
        ],
    )
    conn.commit()
    conn.close()


class Top5SummaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.db_path = Path(self._tmp) / "t.sqlite"
        seed_positions(self.db_path)
        self.db = PositionsDatabase(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_summary_shape_and_keys(self):
        s = top5_daily_summary(self.db, "\u94dc", days=3, top_n=5)
        self.assertEqual(s["product"], "\u94dc")
        self.assertEqual(s["days"], 3)
        self.assertEqual(s["top_n"], 5)
        self.assertEqual(s["anchor_date"], "2026-06-03")
        self.assertEqual(s["trade_dates"], ["2026-06-01", "2026-06-02", "2026-06-03"])
        self.assertEqual(len(s["days_data"]), 3)
        sample = s["days_data"][0]
        for key in ("trade_date", "top_long", "top_short", "long_total", "short_total",
                    "net_long_short", "change_long", "change_short", "change_net"):
            self.assertIn(key, sample)

    def test_first_day_change_is_null_and_subsequent_days_populated(self):
        s = top5_daily_summary(self.db, "\u94dc", days=3, top_n=5)
        # First day has no previous snapshot in window -> all None.
        self.assertIsNone(s["days_data"][0]["change_long"])
        self.assertIsNone(s["days_data"][0]["change_net"])
        # Middle and last day have non-None changes.
        self.assertIsNotNone(s["days_data"][1]["change_long"])
        self.assertIsNotNone(s["days_data"][2]["change_net"])

    def test_top_n_truncates_and_totals_match(self):
        s = top5_daily_summary(self.db, "\u94dc", days=1, top_n=3)
        first = s["days_data"][0]
        self.assertEqual(len(first["top_long"]), 3)
        self.assertEqual(len(first["top_short"]), 3)
        # long_total must equal sum of top_long values (and same for short).
        self.assertAlmostEqual(first["long_total"], sum(r["value"] for r in first["top_long"]))
        self.assertAlmostEqual(first["short_total"], sum(r["value"] for r in first["top_short"]))
        # net_long_short = long_total - short_total.
        self.assertAlmostEqual(first["net_long_short"], first["long_total"] - first["short_total"])

    def test_change_uses_top_n_totals(self):
        # day 0 long total = 100+80+60+40+20 = 300
        # day 1 long total = 105+86+67+48+29 = 335  (each mi grows by 5+mi: +5,+6,+7,+8,+9)
        # day 2 long total = 110+92+74+56+38 = 370  (each mi grows by 10+2*mi: +10,+12,+14,+16,+18)
        s = top5_daily_summary(self.db, "\u94dc", days=2, top_n=5)
        self.assertEqual(s["trade_dates"], ["2026-06-02", "2026-06-03"])
        self.assertAlmostEqual(s["days_data"][0]["long_total"], 335.0)
        self.assertAlmostEqual(s["days_data"][1]["long_total"], 370.0)
        self.assertAlmostEqual(s["days_data"][1]["change_long"], 35.0)

    def test_change_on_first_day_when_prev_day_exists_outside_window(self):
        # days=2 -> window covers day1+day2; day0 fetched separately to compute day1 delta.
        s = top5_daily_summary(self.db, "\u94dc", days=2, top_n=5)
        self.assertEqual(s["trade_dates"], ["2026-06-02", "2026-06-03"])
        self.assertAlmostEqual(s["days_data"][0]["change_long"], 35.0)


class BuildPromptTest(unittest.TestCase):
    def test_prompt_mentions_product_days_and_topn(self):
        summary = {
            "product": "\u94dc", "days": 5, "top_n": 5, "anchor_date": "2026-06-04",
            "trade_dates": ["2026-06-01"], "days_data": [],
        }
        prompt = build_ai_prompt(summary)
        self.assertIn("\u94dc", prompt)
        self.assertIn("5", prompt)
        self.assertIn("5 \u5e2d\u4f4d", prompt)
        self.assertIn("|", prompt)
        self.assertIn("\u7ed3\u7b97\u4ef7", prompt)


class CallDeepseekTest(unittest.TestCase):
    def test_missing_key_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                call_deepseek("hi")

    def test_request_payload_and_headers(self):
        fake_response = MagicMock()
        fake_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        fake_response.raise_for_status.return_value = None
        session = MagicMock()
        session.post.return_value = fake_response
        result = call_deepseek("hello", api_key="sk-test", model="deepseek-chat", http=session)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        # Inspect POST call.
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], DEEPSEEK_API_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")
        payload = json.loads(kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertEqual(payload["messages"][1]["content"], "hello")

    def test_extract_text_handles_missing_keys(self):
        self.assertEqual(extract_text({}), "")
        self.assertEqual(extract_text({"choices": []}), "")
        self.assertEqual(extract_text({"choices": [{"message": {}}]}), "")


if __name__ == "__main__":
    unittest.main()
