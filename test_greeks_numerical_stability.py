"""High-precision decimal references independent of the binary64 pricing paths."""

from decimal import Decimal, localcontext
import math
import unittest

import calculate_greeks as model
import validate_greeks as validator


PI = Decimal("3.141592653589793238462643383279502884197169399375105820974944592307816406286208998628034825342117068")


def decimal_cdf(x):
    half = Decimal("0.5")
    if x > 0:
        return 1 - decimal_cdf(-x)
    if x < -6:
        # Laplace's continued fraction for the normal Mills ratio, rather
        # than the binary64 implementation's far-tail asymptotic polynomial.
        a, fraction = -x, Decimal(0)
        for n in reversed(range(1, 401)):
            fraction = n / (a + fraction)
        return (-x * x / 2).exp() / (2 * PI).sqrt() / (a + fraction)
    u = x / Decimal(2).sqrt()
    term = total = u
    for n in range(1, 300):
        term *= -u * u / n
        total += term / (2 * n + 1)
    return half + total / PI.sqrt()


def decimal_price(kind, s, k, t, r, q, sigma):
    with localcontext() as ctx:
        ctx.prec = 90
        s, k, t, r, q, sigma = map(Decimal, (s, k, t, r, q, sigma))
        width = sigma * t.sqrt()
        d1 = ((s / k).ln() + (r - q) * t) / width + width / 2
        d2 = d1 - width
        ds, dk = s * (-q * t).exp(), k * (-r * t).exp()
        return float(ds * decimal_cdf(d1) - dk * decimal_cdf(d2) if kind == "CALL" else
                     dk * decimal_cdf(-d2) - ds * decimal_cdf(-d1))


class NumericalStabilityTests(unittest.TestCase):
    def test_against_independent_90_digit_reference(self):
        cases = (
            (100, 100, 1e-32, 0, 0, .2),
            (100, 100, 1e-30, 1e14, 0, .2),
            (100, 100, 1e-30, 0, 1e14, .2),
            (100, 100, .7, math.nextafter(.4394, math.inf), .4394, 1e-16),
            (100, math.nextafter(100, math.inf), 1e-29, 0, 0, .2),
            (100, 100, 1, 0, 0, 1e-16),
            (100, 93, 7 / 365, .4394, .08, .37),
            (100, 110, 4, -.04, .05, 5.0),
            (100, 1000, 1, 0, 0, .25),
            (1000, 100, 1, 0, 0, .25),
            (1e280, 5e283, 1, 0, 0, .2),
            (5e283, 1e280, 1, 0, 0, .2),
        )
        for kind in ("CALL", "PUT"):
            for args in cases:
                with self.subTest(kind=kind, args=args):
                    reference = decimal_price(kind, *args)
                    self.assertGreater(reference, 0)
                    self.assertTrue(math.isclose(model.bs_price(kind, *args), reference, rel_tol=2e-11),
                                    (model.bs_price(kind, *args), reference))

    def test_parity_bounds_and_monotonicity_across_regimes(self):
        for args in ((100, 95, 1, -.07, .04), (100, 100, 1e-30, 1e14, 0),
                     (100, 1000, 1, 0, 0), (1000, 100, 1, 0, 0)):
            previous = {"CALL": -1, "PUT": -1}
            s, k, t, r, q = args
            with localcontext() as ctx:
                ctx.prec = 70
                parity = float(Decimal(s) * (-Decimal(q) * Decimal(t)).exp()
                               - Decimal(k) * (-Decimal(r) * Decimal(t)).exp())
            for sigma in (1e-10, .0001, .05, .25, 1, 5, 20):
                with self.subTest(args=args, sigma=sigma):
                    current = {}
                    for kind in previous:
                        price = model.bs_price(kind, *args, sigma)
                        lower, upper = model.price_bounds(kind, *args)
                        self.assertLessEqual(lower, price)
                        self.assertLessEqual(price, upper)
                        self.assertGreaterEqual(price, previous[kind])
                        current[kind] = price
                    self.assertTrue(math.isclose(current["CALL"] - current["PUT"], parity, rel_tol=3e-14))
                    previous = current

    def test_calibration_in_tiny_premium_and_upper_deficit_coordinates(self):
        for kind in ("CALL", "PUT"):
            for years, sigma in ((1e-32, .2), (.6, .43), (10, 4.6)):
                args = (100, 100, years, 0, 0)
                target = decimal_price(kind, *args, sigma)
                with self.subTest(kind=kind, years=years):
                    fitted, reason = model._solve_iv(kind, target, *args)
                    self.assertIsNone(reason)
                    self.assertAlmostEqual(fitted, sigma, delta=5e-5 if years == 10 else 1e-9)
                    self.assertTrue(math.isclose(model.bs_price(kind, *args, fitted), target, rel_tol=1e-10))
            self.assertEqual(model.iv_failure_reason(kind, math.nextafter(100, -math.inf), 100, 100, 10, 0, 0),
                             "near_upper_bound_unidentifiable")
            self.assertEqual(model.iv_failure_reason(kind, math.ulp(0.0), 100, 100, 1, 0, 0),
                             "subnormal_premium_unidentifiable")

    def test_validator_preserves_near_bound_information_after_iv_rounding(self):
        for kind, s, k in (("CALL", 150, 100), ("PUT", 100, 150)):
            args = (s, k, 7 / 365, model.RISK_FREE_RATE, model.DIVIDEND_YIELD)
            lower, _ = model.price_bounds(kind, *args)
            target = lower + 1e-12
            fitted, reason = model._solve_iv(kind, target, *args)
            self.assertIsNone(reason)
            row = {"option_type": kind, "underlying_model_price": s, "strike_price": k,
                   "time_to_expiry_days": 7, "option_mid_price": str(target),
                   "iv_mid": round(100 * fitted, model.IV_PERCENT_DECIMALS)}
            self.assertTrue(validator.reconstruct(row)[2])
            self.assertFalse(validator.reconstruct(row | {"iv_mid": 100 * fitted - 3})[2])
            self.assertEqual(model.iv_failure_reason(kind, math.nextafter(lower, math.inf), *args),
                             "near_lower_bound_unidentifiable")
