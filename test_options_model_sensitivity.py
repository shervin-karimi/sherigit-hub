import unittest

from options_model_sensitivity import GREEKS, scenario


class OptionsModelSensitivityTests(unittest.TestCase):
    def test_fixed_volatility_recovers_iv_from_generated_premium(self):
        for kind in ("CALL", "PUT"):
            for sigma in (0.20, 0.40, 0.80):
                with self.subTest(kind=kind, sigma=sigma):
                    row = scenario("fixed_sigma", kind, sigma=sigma)
                    self.assertAlmostEqual(row["iv"], sigma, places=8)
                    self.assertIsNone(row["iv_failure_reason"])
                    self.assertGreater(row["vega_per_1pct"], 0)

    def test_rate_shock_refits_fixed_premium_in_opposite_directions(self):
        for kind, direction in (("CALL", -1), ("PUT", 1)):
            with self.subTest(kind=kind):
                low = scenario("low", kind, rate=0.30)
                high = scenario("high", kind, rate=0.50)
                self.assertEqual(low["premium"], high["premium"])
                self.assertGreater(direction * (high["iv"] - low["iv"]), 0)

    def test_model_bound_failure_does_not_produce_fitted_greeks(self):
        row = scenario("days_120", "CALL", days=120)
        self.assertIsNone(row["iv"])
        self.assertEqual(row["iv_failure_reason"], "below_theoretical_bound")
        self.assertTrue(all(row[key] is None for key in GREEKS))
