"""Tests for futures_positions.shfe_report."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from futures_positions.shfe_report import build_dashboard_html, build_summary, records


def make_frame() -> pd.DataFrame:
    rows = []
    for trade_date in ("2026-06-01", "2026-06-02", "2026-06-03"):
        for rank, member in enumerate(("会员甲", "会员乙", "会员丙"), 1):
            for metric, value in (("long", 100 * rank), ("short", 80 * rank), ("volume", 180 * rank)):
                rows.append({
                    "trade_date": trade_date,
                    "exchange": "SHFE",
                    "product": "铜",
                    "contract": f"cu{trade_date[2:4]}06",
                    "rank": rank,
                    "member": member,
                    "metric": metric,
                    "value": value,
                    "change": 0,
                    "source_url": "t",
                    "fetched_at": "2026-01-01T00:00:00+08:00",
                })
    return pd.DataFrame(rows)


class ShfeReportTest(unittest.TestCase):
    def test_build_summary_member_product_daily_has_net(self):
        summaries = build_summary(make_frame())
        mp = summaries["member_product_daily"]
        self.assertIn("net", mp.columns)
        self.assertGreater(len(mp), 0)
        # latest day only has 3 members × 1 product = 3 rows
        latest_date = "2026-06-03"
        self.assertEqual(len(summaries["latest_member_product_totals"]), 3)
        self.assertEqual(set(summaries["daily_totals"]["trade_date"]), {"2026-06-01", "2026-06-02", latest_date})

    def test_empty_df_returns_empty_html_no_sidecar(self):
        html, sidecar = build_dashboard_html(df=pd.DataFrame(), summaries={"daily_totals": pd.DataFrame()})
        self.assertIsNone(sidecar)

    def test_sidecar_off_inlines_member_daily(self):
        df = make_frame()
        summaries = build_summary(df)
        html, sidecar = build_dashboard_html(df, summaries, sidecar=False)
        self.assertIsNone(sidecar)
        # full payload is inline (memberDaily non-empty list)
        import re
        self.assertTrue(re.search(r'"memberDaily":\s*\[\s*\{', html),
                        "memberDaily should be inline and non-empty when sidecar=False")

    def test_sidecar_on_strips_member_daily_and_returns_json(self):
        df = make_frame()
        summaries = build_summary(df)
        html, sidecar = build_dashboard_html(df, summaries, sidecar=True)
        self.assertIsNotNone(sidecar)
        # memberDaily must be emptied in the inline payload
        import re
        self.assertTrue(re.search(r'"memberDaily":\s*\[\s*\]', html),
                        "memberDaily must be stripped from inline payload when sidecar=True")
        # loader flag must be enabled
        self.assertIn("fetch('member_daily.json'", html)
        # sidecar is valid JSON and non-empty
        rows = json.loads(sidecar)
        self.assertGreater(len(rows), 0)
        self.assertIn("trade_date", rows[0])
        self.assertIn("member", rows[0])

    def test_export_workbook_writes_sidecar_alongside_html(self):
        from futures_positions.shfe_report import export_workbook
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            df = make_frame()
            log = pd.DataFrame([{"trade_date": "2026-06-01", "status": "ok"}])
            summaries = build_summary(df)
            paths = export_workbook(Path(tmp), df, log, summaries)
            self.assertTrue(paths["html"].exists())
            sidecar = paths["html"].parent / "member_daily.json"
            self.assertTrue(sidecar.exists())
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertGreater(len(data), 0)

    def test_records_converts_dataframe_rows(self):
        df = pd.DataFrame([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
        self.assertEqual(records(df), [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])


if __name__ == "__main__":
    unittest.main()
