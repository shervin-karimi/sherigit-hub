"""Offline mathematical references and known regressions; no market outputs."""

import contextlib
import csv
import io
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import calculate_greeks as model
import rank_candidates
import validate_greeks as validator


def reference_price(kind, spot, strike, years, rate, yield_, volatility):
    """Forward-form price, using erfc to retain deep-tail probabilities."""
    forward = spot * math.exp((rate - yield_) * years)
    width = volatility * math.sqrt(years)
    z = (math.log(forward / strike) - width**2 / 2) / width
    sign = 1 if kind == "CALL" else -1
    n1 = math.erfc(-sign * (z + width) / math.sqrt(2)) / 2
    n2 = math.erfc(-sign * z / math.sqrt(2)) / 2
    return sign * math.exp(-rate * years) * (forward * n1 - strike * n2)


def validator_report(**overrides):
    row = dict(
        symbol="fixture", option_type="CALL", underlying_model_price=100,
        strike_price=100, time_to_expiry_days=365, iv_mid=20,
        option_mid_price=reference_price("CALL", 100, 100, 1, 0.4394, 0, 0.2),
        delta=0.5, gamma=0.02, theta_per_day=-0.02, vega_per_1pct=0.1,
        rho_per_1pct=0.3, risk_free_rate=0.4394, dividend_yield=0,
    )
    row.update(overrides)
    output = io.StringIO()
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "fixture.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        with patch.object(validator, "INPUT", path), contextlib.redirect_stdout(output):
            validator.main()
    return output.getvalue()


class GreeksAudit(unittest.TestCase):
    def test_atm_reference_prices_and_iv(self):
        args = (100, 100, 1, 0.05, 0)
        for kind, expected in (("CALL", 10.450583572185565), ("PUT", 5.573526022256971)):
            with self.subTest(kind=kind):
                self.assertAlmostEqual(model.bs_price(kind, *args, 0.2), expected, places=12)
                self.assertAlmostEqual(model.implied_volatility(kind, expected, *args), 0.2, places=8)

    def test_greeks_against_finite_differences_nonzero_dividend(self):
        for args in ([104, 100, 0.6, 0.07, 0.035, 0.27],
                     [90, 100, 25 / 365, 0.4394, 0, 0.6]):
            with self.subTest(parameters=args):
                self.check_greeks_finite_differences(args)

    def check_greeks_finite_differences(self, args):
        for kind in ("CALL", "PUT"):
            def price(index, shift):
                point = args.copy()
                point[index] += shift
                return reference_price(kind, *point)

            def derivative(index, step):
                return (price(index, step) - price(index, -step)) / (2 * step)

            spot_step = 0.01
            expected = {
                "delta": derivative(0, spot_step),
                "gamma": (price(0, spot_step) - 2 * price(0, 0) + price(0, -spot_step))
                / spot_step**2,
                "theta_per_day": -derivative(2, 0.00001) / 365,
                "rho_per_1pct": derivative(3, 0.00001) / 100,
                "vega_per_1pct": derivative(5, 0.00001) / 100,
            }
            actual = model.calculate_greeks(kind, *args)
            for field, value in expected.items():
                with self.subTest(kind=kind, greek=field):
                    self.assertAlmostEqual(actual[field], value, delta=5e-8)

    def test_parity_scale_and_bid_mid_ask_iv_order(self):
        s, k, t, r, q = 100, 103, 90 / 365, 0.4394, 0.04
        args = (s, k, t, r, q)
        call = model.bs_price("CALL", *args, 0.4)
        put = model.bs_price("PUT", *args, 0.4)
        self.assertAlmostEqual(call - put, s * math.exp(-q * t) - k * math.exp(-r * t), places=12)
        self.assertAlmostEqual(model.bs_price("CALL", s * 10, k * 10, t, r, q, 0.4), 10 * call, places=12)
        for kind in ("CALL", "PUT"):
            bid = reference_price(kind, *args, 0.35)
            ask = reference_price(kind, *args, 0.45)
            ivs = [model.implied_volatility(kind, price, *args) for price in (bid, (bid + ask) / 2, ask)]
            self.assertAlmostEqual(ivs[0], 0.35, places=8)
            self.assertAlmostEqual(ivs[2], 0.45, places=8)
            self.assertLess(ivs[0], ivs[1])
            self.assertLess(ivs[1], ivs[2])

    def test_iv_rejects_nonpositive_prices_expired_and_upper_bounds(self):
        for kind, lower, upper in (
            ("CALL", 100 - 90 * math.exp(-0.05), 100),
            ("PUT", 110 * math.exp(-0.05) - 100, 110 * math.exp(-0.05)),
        ):
            strike = 90 if kind == "CALL" else 110
            for price in (0, -1, lower - 0.1, upper, upper + 1):
                with self.subTest(kind=kind, price=price):
                    self.assertIsNone(model.implied_volatility(kind, price, 100, strike, 1, 0.05, 0))
        self.assertIsNone(model.implied_volatility("CALL", 1, 100, 100, 0, 0.05, 0))
        price = reference_price("CALL", 100, 100, 1, 0.05, 0, 5.1)
        self.assertIsNone(model.implied_volatility("CALL", price, 100, 100, 1, 0.05, 0))

    def test_snapshot_date_takes_priority_in_normal_case(self):
        self.assertEqual(model.get_time_to_expiry({
            "maturity_date": "20261201", "snapshot_received_at": "2026-11-21T10:00:00+03:30",
            "underlying_price_date": "20261120", "remaining_days": 12,
        }), (10 / 365, 10))

    def test_validator_reprices_valid_fixture(self):
        midpoint = reference_price("CALL", 100, 100, 1, 0.4394, 0, 0.2)
        for values in ({},
                       dict(option_mid_price=None, best_bid=midpoint - 0.1, best_ask=midpoint + 0.1),
                       dict(option_mid_price=round(midpoint, 2), best_bid=midpoint, best_ask=midpoint)):
            with self.subTest(values=values):
                report = validator_report(**values)
                self.assertIn("Contracts checked: 1", report)
                self.assertIn("Contracts above tolerance: 0", report)
        report = validator_report(option_mid_price=midpoint + 0.001, symbol=None)
        self.assertIn("Contracts checked: 1", report)
        self.assertIn("Contracts skipped: 0", report)
        self.assertIn("Contracts above tolerance: 1", report)

    def test_unverified_assumptions_keep_rank_closed(self):
        accepted, reason = rank_candidates.check_contract(dict(
            option_type="CALL", delta=0.6, remaining_days=30, spread_percent=1,
            trade_count=30, trade_volume=1, best_bid=99, best_ask=100,
            bid_volume=1, ask_volume=1, ask_time_value=1, iv_status="ok",
            greeks_assumption_status="experimental_unverified",
        ))
        self.assertFalse(accepted)
        self.assertIn("Greeks", reason)

    def test_iv_does_not_report_floor_for_unbracketed_price(self):
        args = (100, 100, 1, 0, 0)
        price = reference_price("CALL", *args, 0.00005)
        self.assertEqual(model.iv_failure_reason("CALL", price, *args), "outside_solver_range")
        self.assertIsNone(model.implied_volatility("CALL", price, *args))
        with patch.object(model, "BISECTION_STEPS", 1):
            price = reference_price("CALL", *args, 0.2)
            self.assertIsNone(model.implied_volatility("CALL", price, *args))
            self.assertEqual(model.iv_failure_reason("CALL", price, *args), "price_reconstruction_failed")

    def test_iv_rejects_nan_price(self):
        self.assertIsNone(model.implied_volatility("CALL", math.nan, 100, 100, 1, 0.05, 0))
        for index in range(6):
            for invalid in (math.nan, math.inf, -math.inf):
                args = [10, 100, 100, 1, 0.05, 0]
                args[index] = invalid
                with self.subTest(index=index, invalid=invalid):
                    self.assertIsNone(model.implied_volatility("CALL", *args))

    def test_deep_otm_call_retains_insignificant_but_positive_tail(self):
        args = ("CALL", 100, 1000, 1, 0, 0, 0.25)
        self.assertTrue(math.isclose(model.bs_price(*args), reference_price(*args), rel_tol=1e-10))
        kind, *inputs, sigma = args
        price = reference_price(*args)
        fitted = model.implied_volatility(kind, price, *inputs)
        self.assertAlmostEqual(fitted, sigma, places=10)
        self.assertTrue(math.isclose(model.bs_price(kind, *inputs, fitted), price, rel_tol=1e-10))
        self.assertEqual(model.bs_price("CALL", 100, 1e100, 1, 0, 0, model.MAX_VOL), 0)
        self.assertIsNone(model.implied_volatility("CALL", 1e-100, 100, 1e100, 1, 0, 0))
        self.assertIsNone(model.implied_volatility("CALL", math.ulp(0.0), *inputs))

    def test_expired_snapshot_cannot_fall_back_to_stale_positive_time(self):
        self.assertEqual(model.get_time_to_expiry({
            "maturity_date": "20261121", "snapshot_received_at": "2026-11-22T10:00:00+03:30",
            "underlying_price_date": "20261120", "remaining_days": 5,
        }), (None, None))
        for snapshot, underlying in (("2026-11-21T09:00:00+03:30", "20261120"),
                                     (None, "20261122")):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(model.get_time_to_expiry({
                    "maturity_date": "20261121", "snapshot_received_at": snapshot,
                    "underlying_price_date": underlying, "remaining_days": 5,
                }), (None, None))

    def test_validator_does_not_count_nan_as_a_checked_row(self):
        self.assertIn("Contracts checked: 0", validator_report(option_mid_price="nan"))
        for values in (dict(iv_mid="inf"), dict(underlying_model_price="-inf"),
                       dict(best_bid="10"), dict(best_bid=41, best_ask=40),
                       dict(best_bid=40, best_ask=42, option_mid_price=40)):
            with self.subTest(values=values):
                report = validator_report(**values)
                self.assertIn("Contracts checked: 0", report)
                self.assertIn("Contracts skipped: 1", report)


if __name__ == "__main__":
    unittest.main()
