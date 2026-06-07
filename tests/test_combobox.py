"""Tests for the searchable combobox injected by shfe_report.

We don't spin up a real browser; instead we assert on the HTML string and
check that:
  1. The combobox markup is present and replaces the old plain <select>.
  2. The combobox JS uses the shared product source (mp2ProductList).
  3. The hidden input keeps the reportBtn submission contract intact.
"""
from __future__ import annotations

import unittest

from futures_positions.shfe_report import build_dashboard_html


class ComboboxHtmlTest(unittest.TestCase):
    def setUp(self):
        self.html, _ = build_dashboard_html(
            payload={"latestDate": "2026-06-04"}, latest_date="2026-06-04"
        )

    def test_combobox_markup_present(self):
        self.assertIn('<div class="combobox" id="reportProductCombobox">', self.html)
        self.assertIn('<input type="text" id="reportProductInput"', self.html)
        self.assertIn('<input type="hidden" id="reportProductSelect"', self.html)
        self.assertIn('<div class="combobox-dropdown" id="reportProductDropdown">', self.html)

    def test_plain_select_replaced(self):
        # The old plain <select id="reportProductSelect"></select> must be gone.
        self.assertNotIn('<select id="reportProductSelect"></select>', self.html)

    def test_combobox_css_present(self):
        # CSS rules for the component.
        self.assertIn(".combobox ", self.html)
        self.assertIn(".combobox-dropdown", self.html)
        self.assertIn(".combobox-option", self.html)

    def test_combobox_js_present(self):
        # Controller IIFE and helpers.
        self.assertIn("const productCombobox", self.html)
        self.assertIn("function initReportProductSelect()", self.html)
        # Keyboard handlers.
        self.assertIn("ArrowDown", self.html)
        self.assertIn("ArrowUp", self.html)
        # Filtering is done against lowercased mp2ProductList().
        self.assertIn("mp2ProductList()", self.html)
        # Hidden input is read by runReport so the form contract is preserved.
        self.assertIn("productCombobox.getValue()", self.html)

    def test_run_report_reads_combobox(self):
        # Ensure runReport consults productCombobox.getValue() rather than the
        # raw element value, so an unconfirmed typed value does not submit.
        self.assertIn("const product = productCombobox.getValue();", self.html)


if __name__ == "__main__":
    unittest.main()
