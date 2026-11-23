"""Offline tests with in-memory daily history, never saved as market data."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
import math
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

import analyze_underlying_trends_v2 as trends


class HistoricalResearchTests(unittest.TestCase):
    code = "65883838195688438"
    fetched = "2026-03-02T08:00:00+03:30"
    before = date(2026, 3, 2)
    first = date(2026, 1, 1)
    meta = {"source": "synthetic test fixture"}

    def item(self, prices):
        return {"code": self.code, "fetched_at": self.fetched, "history": [
            {"dEven": int((self.first + timedelta(days=i)).strftime("%Y%m%d")),
             "insCode": self.code, "pClosing": p, "qTotTran5J": 100 + i,
             "priceMin": p - 1, "priceMax": p + 1}
            for i, p in enumerate(prices)
        ]}

    def report(self, prices=None, *, item=None, before=None, calendar=None):
        return trends.research_report("خودرو", self.item(prices) if item is None else item, self.meta,
                                      before=before or self.before, calendar=calendar, expected_code=self.code)

    def test_increasing_decreasing_and_flat_arithmetic(self):
        for prices, rsi, macd, sma5, sma20 in (
            (list(range(100, 140)), 100, 7, 137, 129.5),
            (list(range(139, 99, -1)), 0, -7, 102, 109.5),
            ([100] * 40, 50, 0, 100, 100),
        ):
            with self.subTest(rsi=rsi):
                indicators = self.report(prices)["indicators"]
                self.assertEqual(indicators["SMA(5)"]["value"], sma5)
                self.assertEqual(indicators["SMA(20)"]["value"], sma20)
                self.assertEqual(indicators["RSI(14)_WILDER"]["value"], rsi)
                self.assertAlmostEqual(indicators["RETURN(5)_PCT"]["value"], 100 * (prices[-1] / prices[-6] - 1))
                self.assertAlmostEqual(indicators["RETURN(20)_PCT"]["value"], 100 * (prices[-1] / prices[-21] - 1))
                self.assertEqual(indicators["MACD(12,26,9)"]["value"],
                                 {"line": macd, "signal": macd, "histogram": 0})
                self.assertEqual(indicators["MACD(12,26,9)"]["warmup"], "LIMITED")
        self.assertEqual(indicators["HV(20)_ANNUALIZED_PCT_SQRT252"]["value"], 0)

    def test_rsi_wilder_update_macd_seed_and_nonzero_volatility(self):
        rsi = self.report([100, 102] * 8)["indicators"]["RSI(14)_WILDER"]["value"]
        self.assertAlmostEqual(rsi, 100 * 15 / 28)
        indicators = self.report([100] * 33 + [112])["indicators"]
        impulse = 12 * (2 / 13 - 2 / 27)
        result = indicators["MACD(12,26,9)"]["value"]
        self.assertAlmostEqual(result["line"], impulse)
        self.assertAlmostEqual(result["signal"], impulse / 9)
        self.assertAlmostEqual(result["histogram"], impulse * 8 / 9)
        self.assertAlmostEqual(indicators["HV(20)_ANNUALIZED_PCT_SQRT252"]["value"],
                               math.log(1.12) * math.sqrt(252 / 20) * 100)

    def test_insufficient_history_and_first_full_macd_value(self):
        for metric, minimum in (("SMA(5)", 5), ("SMA(20)", 20), ("RETURN(5)_PCT", 6),
                                ("RETURN(20)_PCT", 21), ("RSI(14)_WILDER", 15),
                                ("MACD(12,26,9)", 34), ("HV(20)_ANNUALIZED_PCT_SQRT252", 21)):
            with self.subTest(metric=metric):
                short = self.report([100] * (minimum - 1))["indicators"][metric]
                self.assertEqual(short["status"], "INSUFFICIENT_HISTORY")
                self.assertIn(str(minimum), short["reason"])
                self.assertNotIn("value", short)
                self.assertEqual(self.report([100] * minimum)["indicators"][metric]["status"], "COMPUTED")

    def test_out_of_order_rows_sorted_and_listed_gaps_reported_not_filled(self):
        item = self.item([101, 102, 103, 104, 105, 106])
        item["history"].pop(2)
        calendar = {"source": "fixture", "sessions": [f"2026-01-{i:02}" for i in range(1, 7)]}
        report = self.report(item=item, calendar=calendar)
        self.assertEqual(report["missing_listed_sessions"], ["2026-01-03"])
        self.assertEqual(report["session_completeness"], "OPERATOR_CALENDAR_UNVERIFIED")
        self.assertEqual(report["valid_close_count"], 5)
        self.assertEqual(report["indicators"]["SMA(5)"]["value"], (101 + 102 + 104 + 105 + 106) / 5)
        self.assertEqual(report["indicators"]["RETURN(5)_PCT"]["status"], "INSUFFICIENT_HISTORY")
        item["history"].reverse()
        self.assertEqual(self.report(item=item, calendar=calendar), report)

    def test_invalid_dates_duplicates_and_identity_rejected(self):
        for key, value, error in (
            ("dEven", None, "تاریخ"), ("dEven", 20260101.5, "تاریخ"),
            ("dEven", 20260102, "تاریخ تکراری"), ("insCode", "123", "تطابق ندارد"),
        ):
            item = self.item([100] * 2)
            item["history"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, error):
                self.report(item=item)
        item = self.item([100]) | {"code": "123"}
        with self.assertRaisesRegex(ValueError, "متعارض"):
            self.report(item=item)

    def test_invalid_zero_nonfinite_and_boolean_closes_rejected(self):
        for value in (None, 0, -1, float("nan"), float("inf"), float("-inf"), True, "NaN"):
            item = self.item([100])
            item["history"][0]["pClosing"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "قیمت پایانی نامعتبر"):
                self.report(item=item)

    def test_missing_volume_and_ranges_reported_and_zero_volume_not_stitched_in(self):
        item = self.item([100] * 21)
        item["history"][0]["qTotTran5J"] = 0
        item["history"][-1].pop("qTotTran5J")
        item["history"][-1].pop("priceMax")
        report = self.report(item=item)
        self.assertEqual(report["zero_volume_excluded"], ["2026-01-01"])
        self.assertEqual(report["valid_close_count"], 20)
        self.assertEqual(report["indicators"]["SMA(20)"]["value"], 100)
        for metric in ("VOLUME(20)", "RANGE(20)"):
            self.assertEqual(report["indicators"][metric]["status"], "NOT_AVAILABLE")
            self.assertNotIn("value", report["indicators"][metric])
        indicators = self.report(list(range(100, 120)))["indicators"]
        self.assertEqual(indicators["VOLUME(20)"]["value"], {"latest": 119, "mean": 109.5})
        self.assertEqual(indicators["RANGE(20)"]["value"], {"rolling_high": 120, "rolling_low": 99})

    def test_cutoff_excludes_same_day_and_later_prices_and_never_claims_backtest(self):
        item = self.item([100] * 5 + [999, 888])
        report = self.report(item=item, before=date(2026, 1, 6))
        prefix = self.report([100] * 5, before=date(2026, 1, 6))
        self.assertEqual(report["indicators"], prefix["indicators"])
        self.assertEqual(report["excluded_at_or_after_cutoff"], 2)
        # UTC date is Jan 6, but the collection date in Tehran is Jan 7.
        item["fetched_at"] = "2026-01-06T22:00:00+00:00"
        report = self.report(item=item)
        self.assertEqual(report["session_date_cutoff_exclusive"], "2026-01-07")
        self.assertEqual(report["valid_close_count"], 6)
        self.assertEqual(report["historical_availability"], "NOT_VERIFIED_NO_POINT_IN_TIME_BACKTEST")

    def test_unknown_provenance_never_promotes_computed_values(self):
        report = self.report([100] * 40)
        self.assertEqual(report["session_completeness"], "UNKNOWN")
        self.assertIsNone(report["missing_listed_sessions"])
        self.assertEqual(report["adjustment_status"], "UNKNOWN")
        self.assertEqual(report["source_status"], "UNVERIFIED")
        self.assertEqual(report["price_and_volume_units"], "UNVERIFIED")
        self.assertEqual(report["indicators"]["SMA(5)"]["status"], "COMPUTED")
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertFalse(report["execution_eligible"])
        self.assertEqual(report["greeks_assumption_status"], "experimental_unverified")

    def test_import_and_stdout_analysis_have_no_network_or_mutation(self):
        data = json.dumps({"metadata": self.meta, "symbols": {"خودرو": self.item([100] * 40)}})
        out = StringIO()
        with patch("urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP")), \
                patch("socket.create_connection", side_effect=AssertionError("unexpected connection")), \
                patch("sqlite3.connect", side_effect=AssertionError("unexpected ledger or database")):
            runpy.run_path(__file__)
            runpy.run_path(trends.__file__)
            with patch.object(Path, "open", return_value=StringIO(data)) as read, \
                    patch("builtins.open", side_effect=AssertionError("unexpected filesystem access")), \
                    patch.object(Path, "replace", side_effect=AssertionError("unexpected mutation")), \
                    patch.object(trends, "now", return_value=datetime(2026, 3, 2, tzinfo=timezone.utc)), \
                    redirect_stdout(out):
                self.assertEqual(trends.main([]), 0)
            read.assert_called_once_with(encoding="utf-8")
        result = json.loads(out.getvalue())
        self.assertFalse(result["execution_eligible"])
        self.assertEqual(result["valid_close_count"], 40)

    def test_missing_input_explained_without_creating_file(self):
        errors = StringIO()
        with patch.object(Path, "open", side_effect=FileNotFoundError("test missing input")) as read, \
                redirect_stderr(errors):
            self.assertEqual(trends.main([]), 1)
        read.assert_called_once_with(encoding="utf-8")
        self.assertIn("گزارش پژوهشی در دسترس نیست", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
