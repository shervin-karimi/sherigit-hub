"""Offline regression of Theta quality checks using synthetic model scenarios."""

import contextlib
import csv
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import quality_greeks as quality


def fixture(**overrides):
    # European PUT; fixed sigma=20%, 90 days, from the offline sensitivity run.
    row = dict(symbol="synthetic-put", option_type="PUT", underlying_symbol="fixture",
               underlying_model_price=100, strike_price=100, time_to_expiry_days=90,
               risk_free_rate=0.4394, dividend_yield=0, iv_mid=20,
               delta=-0.12701687, gamma=0.02096044, theta_per_day=0.00459607,
               vega_per_1pct=0.10336655, iv_status="ok",
               greeks_assumption_status="experimental_unverified")
    row.update(overrides)
    return row


class ThetaQualityTests(unittest.TestCase):
    def test_positive_put_matches_independent_reference(self):
        # A hypothetical validated flag isolates the numerical quality check;
        # it is not an assertion that the market assumptions have been validated.
        out = quality.review_record(fixture(greeks_assumption_status="validated"))
        self.assertEqual(out["theta_check_status"], "consistent")
        self.assertAlmostEqual(out["theta_reference_per_day"], 0.00459606875794728, delta=5e-8)
        self.assertEqual(out["issues"], "")
        self.assertIn("تتای مثبت", out["warnings"])
        self.assertEqual(out["quality_status"], "عبور از کنترل اولیه")

    def test_negative_theta_scenarios(self):
        for kind, iv, delta, theta in (
            ("CALL", 20, 0.87298313, -0.10342624),
            ("CALL", 40, 0.74046763, -0.10837872),
            ("CALL", 80, 0.68130921, -0.13598932),
            ("PUT", 40, -0.25953237, -0.00035641),
            ("PUT", 80, -0.31869079, -0.02796701),
        ):
            with self.subTest(kind=kind, iv=iv):
                out = quality.review_record(fixture(option_type=kind, iv_mid=iv,
                                                    delta=delta, theta_per_day=theta,
                                                    greeks_assumption_status="validated"))
                self.assertEqual(out["theta_check_status"], "consistent")
                self.assertEqual(out["warnings"], "")
                self.assertEqual(out["issues"], "")

    def test_wrong_positive_and_negative_theta_are_not_accepted(self):
        for value in (0.05, -0.00459607, 0):
            with self.subTest(value=value):
                out = quality.review_record(fixture(theta_per_day=value, greeks_assumption_status="validated"))
                self.assertEqual(out["theta_check_status"], "mismatch")
                self.assertEqual(out["quality_status"], "نیازمند بررسی")
                self.assertEqual(out["warnings"], "")

    def test_unverified_assumptions_stay_flagged_even_with_matching_theta(self):
        source = fixture()
        out = quality.review_record(source)
        self.assertEqual(out["theta_check_status"], "consistent")
        self.assertEqual(out["greeks_assumption_status"], "experimental_unverified")
        self.assertIn("فرض‌های Greeks", out["issues"])
        self.assertEqual(out["quality_status"], "نیازمند بررسی")
        self.assertEqual(source, fixture())

    def test_existing_quality_checks_are_preserved(self):
        out = quality.review_record(fixture(delta=0.5, gamma=-1, vega_per_1pct=-1,
                                             iv_status="failed", greeks_assumption_status="validated"))
        for issue in ("دلتا خارج از دامنه PUT", "گامای منفی", "وگای منفی", "وضعیت IV: failed"):
            self.assertIn(issue, out["issues"])
        self.assertEqual(out["quality_status"], "نیازمند بررسی")
        for iv, issue in ((4, "IV کمتر از ۵٪"), (151, "IV بالاتر از ۱۵۰٪")):
            self.assertIn(issue, quality.review_record(fixture(iv_mid=iv))["issues"])

    def test_missing_model_inputs_are_inconclusive(self):
        for key in ("underlying_model_price", "strike_price", "time_to_expiry_days",
                    "risk_free_rate", "dividend_yield", "iv_mid"):
            with self.subTest(key=key):
                source = fixture(greeks_assumption_status="validated")
                del source[key]
                out = quality.review_record(source)
                self.assertEqual(out["theta_check_status"], "unverified")
                self.assertEqual(out["quality_status"], "نیازمند بررسی")
                self.assertIsNone(out["theta_reference_per_day"])

    def test_invalid_and_unresolved_numeric_inputs(self):
        for changes in (dict(time_to_expiry_days=0), dict(underlying_model_price=math.inf),
                        dict(risk_free_rate=math.nan), dict(dividend_yield=1e308),
                        dict(time_to_expiry_days=1e-300), dict(strike_price=-1),
                        dict(iv_mid=501), dict(option_type="UNKNOWN")):
            with self.subTest(changes=changes):
                out = quality.review_record(fixture(**changes))
                self.assertEqual(out["theta_check_status"], "unverified")
                self.assertIsNone(out["theta_reference_per_day"])
        for theta in (None, math.nan, math.inf):
            self.assertEqual(quality.review_record(fixture(theta_per_day=theta))["theta_check_status"], "missing")

    def test_report_preserves_original_fields_and_separates_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            out_json = root / "report.json"
            out_csv = root / "report.csv"
            original = [fixture(greeks_assumption_status="validated"), fixture()]
            source.write_text(json.dumps(original), encoding="utf-8")
            before = source.read_bytes()
            output = io.StringIO()
            with patch.multiple(quality, INPUT=source, OUTPUT_JSON=out_json, OUTPUT_CSV=out_csv), contextlib.redirect_stdout(output):
                quality.main()
            self.assertEqual(source.read_bytes(), before)
            rows = json.loads(out_json.read_text(encoding="utf-8"))
            with out_csv.open(encoding="utf-8-sig", newline="") as stream:
                csv_rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(csv_rows), 2)
            self.assertEqual(set(rows[0]), set(csv_rows[0]))
            self.assertEqual(list(rows[0])[:14], ["symbol", "option_type", "underlying_symbol", "iv_mid",
                "delta", "gamma", "theta_per_day", "vega_per_1pct", "spread_percent", "trade_count",
                "iv_status", "greeks_assumption_status", "quality_status", "issues"])
            self.assertIn("هشدارهای غیرمسدودکننده: 2", output.getvalue())
            self.assertIn("نیازمند بررسی: 1", output.getvalue())
            self.assertEqual(csv_rows[0]["theta_check_status"], "consistent")


if __name__ == "__main__":
    unittest.main()
