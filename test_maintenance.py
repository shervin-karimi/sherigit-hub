"""Regression checks for offline maintenance and legacy dashboard rendering."""

import unittest

from app import HTML
from sensitivity_test import RATES, sensitivity


class MaintenanceTests(unittest.TestCase):
    def test_sensitivity_uses_in_memory_call_and_put_inputs(self):
        rows = [
            {"symbol": side, "option_type": side, "underlying_closing_price": 100,
             "strike_price": 100, "time_to_expiry_days": 90, "option_mid_price": 20}
            for side in ("CALL", "PUT")
        ]
        results = sensitivity(rows + [{"option_type": "PUT"}])
        self.assertEqual([(r["symbol"], r["rate"]) for r in results],
                         [(side, rate) for side in ("CALL", "PUT") for rate in RATES])
        self.assertTrue(all(row["iv"] is not None for row in results))

    def test_scan_cards_use_text_nodes_and_dom_attributes(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write("):
            self.assertNotIn(sink, HTML)
        self.assertIn("node.textContent=text", HTML)
        self.assertIn("node.dataset.targetKey=x.type+':'+x.symbol", HTML)
        self.assertIn("renderCards('calls',d.calls)", HTML)
        self.assertIn("renderCards('puts',d.puts)", HTML)
