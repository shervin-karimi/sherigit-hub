"""Synthetic-only rate curve research checks."""
import json
import math
import unittest
from unittest.mock import patch
import rate_curve_research as rates


class RateCurveResearchTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = rates.demo_snapshot()
        self.as_of = "2026-11-28"

    def report(self, days=(7, 60, 90, 180, 181)):
        return rates.report(json.dumps(self.snapshot).encode(), self.as_of, days)

    def test_nodes_from_price_and_redemption(self):
        result = self.report()
        n = result["nodes"][0]
        self.assertAlmostEqual(n["rate_continuous"], math.log(1000000 / 940000) * 365 / 63)
        self.assertEqual(n["days"], 60)
        self.assertAlmostEqual(result["estimates"][1]["rate_effective_annual"], math.expm1(n["rate_continuous"]))

    def test_interpolates_log_discount(self):
        result = self.report()
        a, b = result["nodes"]
        mid = result["estimates"][2]
        self.assertAlmostEqual(math.log(mid["discount_factor"]), .75 * a["log_discount"] + .25 * b["log_discount"])
        self.assertEqual(mid["node_ids"], [a["id"], b["id"]])

    def test_no_extrapolation(self):
        estimates = self.report()["estimates"]
        self.assertEqual(estimates[0], {"days": 7, "status": "outside_curve_coverage"})
        self.assertEqual(estimates[-1], {"days": 181, "status": "outside_curve_coverage"})

    def test_rejects_future_and_same_day_availability(self):
        for day in ("2026-11-28", "2026-11-29", "2026-11-24"):
            with self.subTest(day=day):
                self.snapshot["available_date"] = day
                with self.assertRaises(ValueError):
                    self.report()

    def test_rejects_stale_snapshot(self):
        self.as_of = "2026-11-30"
        with self.assertRaisesRegex(ValueError, "stale"):
            self.report()

    def test_rejects_unsupported_or_invalid_bonds(self):
        for key, value in (("close_irr", 0), ("close_irr", True), ("close_irr", float("nan")),
                           ("redemption_irr", -1), ("trade_volume", 0), ("terms_source", ""),
                           ("cashflow_type", "coupon"), ("issuer_type", "corporate"),
                           ("maturity_date", self.as_of), ("quote_date", "2026-11-24")):
            with self.subTest(key=key, value=value):
                self.snapshot = rates.demo_snapshot()
                self.snapshot["bonds"][0][key] = value
                with self.assertRaises(ValueError):
                    self.report()

    def test_duplicate_ids_and_maturities(self):
        for key in ("id", "maturity_date"):
            with self.subTest(key=key):
                self.snapshot = rates.demo_snapshot()
                self.snapshot["bonds"][1][key] = self.snapshot["bonds"][0][key]
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    self.report()

    def test_no_promotion_and_no_network(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("network")), patch("socket.socket", side_effect=AssertionError("network")):
            a = self.report()
            self.assertEqual(a, self.report())
        self.assertFalse(a["execution_eligible"])
        self.assertEqual(a["greeks_assumption_status"], "experimental_unverified")
        self.snapshot["classification"] = "validated"
        with self.assertRaises(ValueError):
            self.report()

    def test_invalid_tenors(self):
        for days in (0, -1, True, 1.5):
            with self.subTest(days=days):
                with self.assertRaises(ValueError):
                    self.report((days,))

    def test_reject_duplicate_json_keys(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            rates.report(b'{"quote_date":"a","quote_date":"b"}', self.as_of, [90])


if __name__ == "__main__":
    unittest.main()
