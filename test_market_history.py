"""Offline tests with in-memory daily history, never saved as market data."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from io import BytesIO, StringIO
import json
import math
import os
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError

import analyze_underlying_trends_v2 as trends
import fetch_underlying_history as fetch


CSV_HEADER = (
    b"<TICKER>,<DTYYYYMMDD>,<FIRST>,<HIGH>,<LOW>,<CLOSE>,<VALUE>,<VOL>,<OPENINT>,<PER>,<OPEN>,<LAST>\r\n"
)
AHRAM_SHA256 = "0b7c2113370d2bebf7ef9d93e88ae333f526278ab3b6e04cf3ecc9e222bce0d5"


def csv_row(day, *, zero=False, unusual=False):
    if zero:
        fields = (" 11690.00" if unusual else " 0.00", " 0.00", " 0.00",
                  " 11310.00", "0.00", " 0", "0", "D", "11310.00", "11340.00")
    else:
        fields = (" 99.125", " 104.00", " 98.00", " 100.00", "1000.00", " 10", "2",
                  "D", "115.00", "103.00")
    return ",".join(("Sample ETF", day.strftime("%Y%m%d"), *fields)).encode() + b"\n"


def csv_export(lines, *, blank=True):
    return CSV_HEADER + (b"\n" if blank else b"") + b"".join(lines)


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


def daily_rows(code, *, fund_like=False):
    return [{"insCode": int(code) if fund_like else code,
             "dEven": int((date(2026, 1, 1) + timedelta(days=i)).strftime("%Y%m%d")),
             "priceFirst": 99, "priceMax": 102, "priceMin": 98, "pClosing": 100,
             "qTotTran5J": 1000 if fund_like else 10, "last": False}
            for i in range(24)]


def provider_body(rows):
    return json.dumps({"closingPriceDaily": rows}).encode()


class HistoryCollectorTests(unittest.TestCase):
    code = HistoricalResearchTests.code
    received = datetime(2026, 2, 2, tzinfo=timezone.utc)
    newer = "80001"

    def guard(self, name, **kwargs):
        patcher = patch(name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stream = self.root / "manual"
        self.stream.mkdir()
        self.history = self.root / "history.json"
        self.kwargs = {"stream": self.stream, "history_path": self.history}
        old = daily_rows(self.code)
        old[0].update(qTotTran5J=0, priceFirst=0, priceMin=0, priceMax=0)
        self.old_entry = {"code": self.code, "fetched_at": self.received.isoformat(), "history": old,
                          "record_count": len(old), "provenance": {"source": "original fixture"}}
        self.baseline = json.dumps({"metadata": {"source": "original fixture", "symbol_count": 1,
                                                 "fetched_at": self.received.isoformat()},
                                    "symbols": {"خودرو": self.old_entry}}, ensure_ascii=False).encode()
        self.history.write_bytes(self.baseline)
        for name in ("socket.socket", "socket.getaddrinfo", "sqlite3.connect",
                     "shadow_collector.fetch", "paper_trading.PaperTrading.buy",
                     "paper_trading.PaperTrading.sell"):
            self.guard(name, side_effect=AssertionError(f"unexpected action: {name}"))
        self.guard("fetch_underlying_history.datetime").now.return_value = self.received
        self.opener = self.guard("fetch_underlying_history.build_opener")
        self.open = self.opener.return_value.open
        self.sleep = self.guard("fetch_underlying_history.time.sleep")
        self.append(1, [self.code, *(str(80001 + i) for i in range(22)), self.newer])
        self.append(2, ["90001"])
        self.guard("shadow_watch.contract_states", side_effect=AssertionError("unexpected screening replay"))

    def append(self, day, codes):
        at = datetime(2026, 2, day, 8, tzinfo=timezone.utc)
        raw = fetch.collector.json_bytes({"instrumentOptMarketWatch": [
            {"uaInsCode": code, "lval30_UA": "خودرو" if code == self.code else f"صندوق نمونه {i}",
             "insCode_C": str(110000 + i), "insCode_P": str(120000 + i),
             "strikePrice": 100, "contractSize": 1000, "endDate": "20270401"}
            for i, code in enumerate(codes)
        ]})
        with fetch.collector.locked(self.stream, exclusive=True) as fd, patch("shadow_watch.contract_states"):
            return fetch.collector.append(self.stream, fd, fetch.collector.read_stream(self.stream),
                                          raw, at - timedelta(seconds=1), at, {})

    def response(self, code, body=None, *, media="application/json; charset=utf-8", url=None, status=200):
        response = MagicMock(status=status)
        response.__enter__.return_value = response
        response.geturl.return_value = url or f"{fetch.BASE_URL}/{code}/40"
        response.headers = Message()
        response.headers["Content-Type"] = media
        response.read.return_value = provider_body(daily_rows(code)) if body is None else body
        return response

    def observation(self, code, names=("نماد تازه",), raw=None):
        return fetch.Observation(code, names, provider_body(daily_rows(code)) if raw is None else raw,
                                 self.received.isoformat(), "manual:1", "a" * 64)

    def test_preview_discovers_all_exact_ids_without_replay_or_network(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = fetch.preview("manual:1", **self.kwargs)
        instruments = report["instruments"]
        self.assertEqual(len(instruments), 23)
        self.assertEqual(len(instruments[self.newer]["symbols"]), 2)
        self.assertNotIn("90001", instruments)
        self.assertEqual(report["available_count"], 1)
        self.assertEqual(instruments[self.code]["technical_analysis_status"], "AVAILABLE")
        self.assertEqual(instruments[self.code]["valid_closes_before_cutoff"], 23)
        self.assertEqual(len(report["missing_ids"]), 22)
        for code in report["missing_ids"]:
            self.assertEqual(instruments[code]["reason_code"], "history_id_not_found")
            self.assertEqual(instruments[code]["technical_analysis_status"], "NOT_AVAILABLE")
            self.assertEqual(instruments[code]["instrument_type"], "UNKNOWN")
            self.assertFalse(instruments[code]["instrument_type_verified"])
        self.assertEqual(list(fetch.preview("manual:2", **self.kwargs)["instruments"]), ["90001"])
        for bad in ("manual:3", "latest", "manual:01", "legacy:../1"):
            with self.subTest(capture=bad), self.assertRaises(ValueError):
                fetch.preview(bad, **self.kwargs)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(set(p for p in self.root.rglob("*") if p.is_file()), set(before))
        self.opener.assert_not_called()

    def test_preview_cli_default_import_and_disabled_collection_do_not_write(self):
        with patch.object(Path, "write_bytes", side_effect=AssertionError("unexpected write")), \
                patch.object(Path, "replace", side_effect=AssertionError("unexpected replacement")), \
                redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
            runpy.run_path(fetch.__file__)
            self.assertEqual(fetch.main([]), 0)
            self.assertIn("preview", out.getvalue())
            self.assertEqual(fetch.main(["collect", "--capture", "manual:1"]), 2)
            self.assertIn("غیرفعال", err.getvalue())
            out.truncate(0)
            out.seek(0)
            self.assertEqual(fetch.main(["preview", "--capture", "manual:1", "--stream", str(self.stream),
                                         "--history", str(self.history)]), 0)
            self.assertEqual(json.loads(out.getvalue())["available_count"], 1)
        self.opener.assert_not_called()

    def test_stock_and_fund_shaped_responses_and_zero_volume_are_preserved(self):
        for code, fund_like in ((self.code, False), (self.newer, True)):
            rows = daily_rows(code, fund_like=fund_like)
            rows[0].update(qTotTran5J=0, priceFirst=0, priceMin=0, priceMax=0)
            rows[1]["qTotTran5J"] = 0
            with self.subTest(code=code):
                normalized = fetch.validate_history(provider_body(rows), code, self.received.isoformat())
                self.assertEqual(len(normalized), len(rows))
                self.assertEqual(normalized[-1]["insCode"], code)
                self.assertFalse(normalized[-1]["_quality"]["ohlc_valid"])
                self.assertTrue(normalized[-2]["_quality"]["ohlc_valid"])
                self.assertFalse(normalized[-2]["_quality"]["volume_valid"])
                self.assertEqual(normalized[-1]["qTotTran5J"], 0)
                self.assertEqual(normalized[0]["last"], False)

    def test_strict_daily_records_fail_closed(self):
        cases = [("dEven", None), ("dEven", 20260230), ("dEven", 20260101.5), ("dEven", 20260102),
                 ("dEven", 20260301), ("insCode", None), ("insCode", True), ("insCode", 80003),
                 ("insCode", 80001.0), ("insCode", "080001"), ("priceFirst", 103), ("priceMin", 102),
                 ("priceMax", 0), ("priceMin", None), ("pClosing", True), ("pClosing", "100"),
                 ("pClosing", float("nan")), ("priceMax", float("inf")),
                 ("qTotTran5J", -1), ("qTotTran5J", False), ("qTotTran5J", None)]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                rows = daily_rows(self.newer)
                rows[0][key] = value
                fetch.validate_history(provider_body(rows), self.newer, self.received.isoformat())
        for raw in (b"", b"[]", b'{"closingPriceDaily": []}', b'{"closingPriceDaily": {}}',
                    b'{"closingPriceDaily": [], "closingPriceDaily": []}', b'{"extra": 1e999}',
                    provider_body(daily_rows(self.newer)[:20]), provider_body([{}] * 41)):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                fetch.validate_history(raw, self.newer, self.received.isoformat())
        rows = daily_rows(self.newer)
        rows.append(rows[0].copy())  # Identical duplicate also fails.
        with self.assertRaisesRegex(ValueError, "duplicate"):
            fetch.validate_history(provider_body(rows), self.newer, self.received.isoformat())

    def test_ohlc_rejects_confirmed_integer_rounding_regression(self):
        n = 2**53
        rows = [dict(row, pClosing=n + 1, priceFirst=n, priceMin=n, priceMax=n)
                for row in daily_rows(self.newer)[:21]]
        self.assertEqual(float(n + 1), float(n))  # Previous validation concealed the violation.
        with self.assertRaisesRegex(ValueError, "inconsistent OHLC"):
            fetch.validate_history(provider_body(rows), self.newer, self.received.isoformat())
        for key, value in (("priceFirst", n + 1), ("priceMin", n + 1)):
            boundary = [dict(row, pClosing=n, **{key: value}) for row in rows]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "inconsistent OHLC"):
                fetch.validate_history(provider_body(boundary), self.newer, self.received.isoformat())

    def test_ohlcv_accepts_exact_integer_boundaries_not_unsafe_range(self):
        n = 2**53
        rows = [dict(row, pClosing=n - 1, priceFirst=n, priceMin=n - 1, priceMax=n, qTotTran5J=n)
                for row in daily_rows(self.newer)]
        normalized = fetch.validate_history(provider_body(rows), self.newer, self.received.isoformat())
        self.assertEqual(normalized[0]["pClosing"], n - 1)
        self.assertIs(type(normalized[0]["priceMax"]), int)
        self.assertEqual(normalized[0]["priceMax"], n)
        self.assertTrue(normalized[0]["_quality"]["ohlc_valid"])
        for field in ("priceMax", "qTotTran5J"):
            too_large = [dict(row, **{field: n + 1}) for row in rows]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "exact numeric range"):
                fetch.validate_history(provider_body(too_large), self.newer, self.received.isoformat())

    def test_decimal_ohlcv_round_trips_or_fails_without_rounding(self):
        rows = [dict(row, pClosing=100.125, priceFirst=100.1, priceMin=100.1, priceMax=100.125, qTotTran5J=0.1)
                for row in daily_rows(self.newer)]
        raw = provider_body(rows)
        normalized = fetch.validate_history(raw, self.newer, self.received.isoformat())
        self.assertEqual(normalized[0]["priceFirst"], 100.1)
        self.assertEqual(normalized[0]["pClosing"], 100.125)
        self.assertTrue(normalized[0]["_quality"]["ohlc_valid"])
        with self.assertRaisesRegex(ValueError, "inconsistent OHLC"):
            fetch.validate_history(raw.replace(b'"pClosing": 100.125', b'"pClosing": 100.12500000000001'),
                                   self.newer, self.received.isoformat())
        for value, reason in ((b"100.1250000000000000001", "decimal precision"),
                              (b"1e-1000", "decimal precision"), (b"-1e-1000", "decimal precision"),
                              (b"1e999", "invalid JSON"), (b"NaN", "invalid JSON"),
                              (b"Infinity", "invalid JSON"), (b"100.12.5", "invalid JSON")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, reason):
                fetch.validate_history(raw.replace(b'"pClosing": 100.125', b'"pClosing": ' + value),
                                       self.newer, self.received.isoformat())

    def test_http_rejects_wrong_content_redirect_and_oversized_or_truncated_body(self):
        oversized = self.response(self.newer, body=b"x" * (fetch.MAX_RESPONSE + 1))
        declared = self.response(self.newer)
        declared.headers["Content-Length"] = str(fetch.MAX_RESPONSE + 1)
        truncated = self.response(self.newer, body=b"{}")
        truncated.headers["Content-Length"] = "5"
        for response, error in ((oversized, "too large"), (declared, "too large"), (truncated, "length mismatch"),
                                (self.response(self.newer, media="text/html"), "content type"),
                                (self.response(self.newer, url="https://other.invalid/"), "redirect"),
                                (self.response(self.newer, status=302), "redirect")):
            self.open.return_value = response
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                fetch.request_history(self.opener.return_value, self.newer, 10)
        oversized.read.assert_called_once_with(fetch.MAX_RESPONSE + 1)
        declared.read.assert_not_called()

    def test_collection_stops_on_access_errors_and_never_retries(self):
        for status in (301, 302, 401, 403, 429):
            with self.subTest(status=status):
                self.open.reset_mock()
                remote = BytesIO()
                self.open.side_effect = HTTPError(fetch.BASE_URL, status, "fixture", {}, remote)
                batch = fetch.collect_missing("manual:1", ["80002", "80003"], max_requests=2, **self.kwargs)
                self.assertTrue(batch["stopped"])
                self.assertIn(f"HTTP {status}", batch["failures"]["80002"])
                self.assertEqual(batch["merge"].replacement, self.baseline)
                self.open.assert_called_once()
                self.assertTrue(remote.closed)
                self.sleep.assert_not_called()
                _, redirect = self.opener.call_args.args
                self.assertIsNone(redirect.redirect_request(None, None, status, "", {}, "https://other.invalid/"))

    def test_budget_and_explicit_missing_identity_checks_precede_http(self):
        for codes, settings in (([], {}), (["80002", "80003"], {}), (["80002"], {"max_requests": 1000}),
                                (["80002"], {"delay": 59}), (["80002"], {"delay": float("nan")}),
                                (["80002"], {"timeout": 0}), (["80002"], {"timeout": 31}),
                                ([self.code], {}), (["90001"], {}), (["80002", "80002"], {"max_requests": 2}),
                                (["نماد"], {})):
            with self.subTest(codes=codes, settings=settings), self.assertRaises(ValueError):
                fetch.collect_missing("manual:1", codes, **({"max_requests": 1} | settings | self.kwargs))
        self.opener.assert_not_called()
        self.sleep.assert_not_called()

    def test_partial_success_only_adds_validated_history_and_respects_cutoff(self):
        invalid = self.response("80002", body=b"{")
        unavailable = HTTPError(fetch.BASE_URL, 404, "fixture", {}, BytesIO())
        rows = daily_rows("80003", fund_like=True)
        success = self.response("80003", body=provider_body(rows))
        events = []

        def open_once(request, *, timeout):
            code = request.full_url.split("/")[-2]
            events.append((code, timeout))
            if code == "80004":
                raise unavailable
            return {"80002": invalid, "80003": success}[code]

        self.open.side_effect = open_once
        self.sleep.side_effect = lambda seconds: events.append(("delay", seconds))
        batch = fetch.collect_missing("manual:1", ["80002", "80003", "80004"], max_requests=3,
                                      delay=65, timeout=12, **self.kwargs)
        self.assertEqual(events, [("80002", 12), ("delay", 65), ("80003", 12),
                                  ("delay", 65), ("80004", 12)])
        self.assertEqual(self.sleep.call_args_list, [call(65), call(65)])
        self.assertFalse(batch["stopped"])
        self.assertEqual(set(batch["failures"]), {"80002", "80004"})
        document = json.loads(batch["merge"].replacement)
        entries = {item["code"]: item for item in document["symbols"].values()}
        self.assertEqual(set(entries), {self.code, "80003"})
        self.assertEqual(entries[self.code], self.old_entry)
        self.assertEqual(document["metadata"], json.loads(self.baseline)["metadata"] | {"symbol_count": 2})
        self.assertEqual(entries["80003"]["provenance"]["body_sha256"], fetch.watch.digest(provider_body(rows)))
        self.assertEqual(entries["80003"]["provenance"]["instrument_type"], "UNKNOWN")
        self.assertEqual(batch["merge"].verify_source(self.history), self.baseline)
        technical = trends.research_report("fixture", entries["80003"], document["metadata"],
                                          before=date(2026, 1, 6), expected_code="80003")
        self.assertEqual(technical["valid_close_count"], 5)
        self.assertEqual(technical["session_date_cutoff_exclusive"], "2026-01-06")
        self.assertFalse(technical["execution_eligible"])
        self.assertEqual(self.history.read_bytes(), self.baseline)

    def test_merge_rejects_conflicts_and_reader_limit_preserves_exact_noop(self):
        obs = self.observation(self.newer)
        self.assertEqual(fetch.prepare_merge(self.baseline, []).replacement, self.baseline)
        for observations in ([obs, obs], [self.observation(self.code)], [self.observation(self.newer, ("خودرو",))],
                             [self.observation(self.newer, raw=b"{}")]):
            with self.subTest(count=len(observations)), self.assertRaises(ValueError):
                fetch.prepare_merge(self.baseline, observations)
        ambiguous = json.loads(self.baseline)
        ambiguous["symbols"]["دیگر"] = self.old_entry
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            fetch.prepare_merge(json.dumps(ambiguous).encode(), [obs])
        with patch.object(fetch.watch, "MAX_BODY", len(self.baseline)):
            with self.assertRaisesRegex(ValueError, "reader limit"):
                fetch.prepare_merge(self.baseline, [obs])

    def test_merge_source_recheck_and_atomic_replacement_design_only_on_fixture(self):
        plan = fetch.prepare_merge(self.baseline, [self.observation(self.newer)])
        # Simulate the future approved writer, not a production write path.
        backup, staged = self.root / "backup", self.root / "staged"
        with fetch.collector.locked(self.root, exclusive=True):
            fetch.collector.sync_write(backup, plan.verify_source(self.history))
            fetch.collector.sync_write(staged, plan.replacement)
            changed = self.baseline + b"\n"
            self.history.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "changed"):
                plan.verify_source(self.history)
            self.assertEqual(self.history.read_bytes(), changed)
            self.history.write_bytes(self.baseline)
            plan.verify_source(self.history)
            os.replace(staged, self.history)
        self.assertEqual(backup.read_bytes(), self.baseline)
        self.assertEqual(self.history.read_bytes(), plan.replacement)
        self.assertFalse(staged.exists())

    def test_concurrent_change_during_batch_prevents_preparing_merge(self):
        def receive(*args, **kwargs):
            self.history.write_bytes(self.baseline + b"\n")
            return self.response("80002")

        self.open.side_effect = receive
        with self.assertRaisesRegex(ValueError, "changed"):
            fetch.collect_missing("manual:1", ["80002"], max_requests=1, **self.kwargs)
        self.open.assert_called_once()

    def import_input(self, code="80002", raw=None):
        source = self.root / "provider.json"
        if raw is None:
            raw = provider_body(daily_rows(code, fund_like=True))
        source.write_bytes(raw)
        return dict(source=source, source_url=f"{fetch.BASE_URL}/{code}/40", source_sha256=fetch.watch.digest(raw),
                    received_at=self.received.isoformat(), code=code, capture_id="manual:1", history=self.history,
                    backup=self.root / "history.backup", stream=self.stream)

    def import_cli(self, inputs):
        options = {"ins-code": inputs["code"], "capture": inputs["capture_id"],
                   **{name.replace("_", "-"): inputs[name] for name in
                      ("source", "source_url", "source_sha256", "received_at", "history", "backup", "stream")}}
        return ["import", *(part for key, value in options.items() for part in (f"--{key}", str(value)))]

    def test_local_import_cli_defaults_to_read_only_and_reports_coverage(self):
        inputs = self.import_input()
        args = self.import_cli(inputs)
        real_open = os.open

        def readonly_open(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            return real_open(path, flags, *args, **kwargs)

        with patch.object(fetch.os, "open", side_effect=readonly_open), \
                patch.object(fetch.os, "replace", side_effect=AssertionError("unexpected rename")), \
                redirect_stdout(StringIO()) as out:
            self.assertEqual(fetch.main(args), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["destination"], str(self.history))
        self.assertEqual(report["backup_required"], str(inputs["backup"]))
        self.assertEqual(report["expected_history_sha256"], fetch.watch.digest(self.baseline))
        self.assertEqual(report["capture_coverage"], {
            "total": 23, "before_available": 1, "after_available": 2,
            "instrument_before": {"status": "NOT_AVAILABLE", "reason_code": "history_id_not_found",
                                  "valid_closes_before_cutoff": None},
            "instrument_after": {"status": "AVAILABLE", "reason_code": "retrospective_history",
                                 "valid_closes_before_cutoff": 24},
        })
        provenance = report["provenance"]
        self.assertEqual(provenance["body_sha256"], inputs["source_sha256"])
        self.assertIsNone(provenance["http_status"])
        self.assertIn("seal_sha256", provenance)
        self.assertEqual(provenance["source_evidence"], "OPERATOR_SUPPLIED_RECEIPT_AND_URL")
        self.assertFalse(report["point_in_time_verified"])
        self.assertFalse(report["execution_eligible"])
        self.assertFalse(inputs["backup"].exists())
        self.assertEqual(self.history.read_bytes(), self.baseline)
        self.opener.assert_not_called()

    def test_offline_merge_plan_preserves_literal_existing_entries_and_original_bytes(self):
        inputs = self.import_input(raw=provider_body(daily_rows("80002")))
        # Preserve original escapes, spacing, and noncanonical numeric representation.
        old_value = json.dumps(self.old_entry, ensure_ascii=True, separators=(", ", " : ")).replace(
            '"pClosing" : 100', '"pClosing" : 1.000e2', 1)
        unrelated = dict(self.old_entry, code="80003", history=daily_rows("80003"))
        other_token = json.dumps(unrelated, ensure_ascii=True, indent=1)
        metadata = b'{ "source" : "old", "symbol_count": 2 }'
        baseline = (b' \r\n{"symbols": {"\\u062e\\u0648\\u062f\\u0631\\u0648" : ' + old_value.encode()
                    + b', "\\u062f\\u06cc\\u06af\\u0631" : ' + other_token.encode()
                    + b' }, "metadata":' + metadata + b', "untouched":"\\u0061"} \r\n')
        self.history.write_bytes(baseline)
        _, bases = fetch.capture_inventory("manual:1", stream=self.stream)
        observation = self.observation("80002", tuple(sorted(bases["80002"])), inputs["source"].read_bytes())
        written = fetch.prepare_merge(baseline, [observation]).replacement
        self.assertIn(old_value.encode(), written)
        self.assertIn(other_token.encode(), written)
        self.assertIn(b'"untouched":"\\u0061"', written)
        self.assertEqual(json.loads(written)["symbols"]["خودرو"], self.old_entry)
        self.assertEqual(json.loads(written)["symbols"]["دیگر"], unrelated)
        self.assertEqual(self.history.read_bytes(), baseline)
        self.assertFalse(inputs["backup"].exists())
        self.assertEqual(list(self.root.glob(".history-*.tmp")), [])
        self.opener.assert_not_called()

    def test_import_into_more_than_eighteen_existing_underlyings(self):
        doc = json.loads(self.baseline)
        for i in range(3, 22):
            code = str(80000 + i)
            doc["symbols"][f"برچسب {i}"] = dict(self.old_entry, code=code, history=daily_rows(code))
        self.history.write_text(json.dumps(doc))
        report = fetch.local_import(**self.import_input())
        self.assertEqual(report["capture_coverage"]["before_available"], 20)
        self.assertEqual(report["capture_coverage"]["after_available"], 21)

    def test_import_rejects_source_identity_provenance_and_payload_errors(self):
        cases = (("source_url", "https://other.invalid/history", "source URL"),
                 ("source_sha256", "", "provider SHA-256"),
                 ("source_sha256", "a" * 64, "payload SHA-256"),
                 ("received_at", None, "receipt timestamp"),
                 ("received_at", "2026-02-02T01:00:00", "receipt timestamp"),
                 ("received_at", "2026-02-01T23:00:00-00:00", "receipt timestamp"),
                 ("received_at", "2026-02-30T01:00:00+03:30", "timestamp"),
                 ("received_at", "2026-03-03T01:00:00+03:30", "future"),
                 ("code", "080002", "stable instrument ID"),
                 ("capture_id", "manual:2", "absent from selected capture"))
        for field, value, reason in cases:
            inputs = self.import_input()
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, reason):
                fetch.local_import(**(inputs | {field: value}))
        invalid_rows = daily_rows("80002")
        invalid_rows[-1]["insCode"] = "80003"
        dup = daily_rows("80002")
        dup[-1]["dEven"] = dup[0]["dEven"]
        numeric = daily_rows("80002")
        numeric[-1]["qTotTran5J"] = True
        for raw, reason in ((provider_body(invalid_rows), "insCode mismatch"),
                            (provider_body(dup), "duplicate session"),
                            (provider_body(numeric), "invalid numeric"),
                            (b"{" + b"x" * fetch.MAX_RESPONSE, "size limit"),
                            (b'{"closingPriceDaily": NaN}', "invalid JSON")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                fetch.local_import(**self.import_input(raw=raw))
        self.assertEqual(self.history.read_bytes(), self.baseline)
        self.opener.assert_not_called()

    def test_import_rejects_existing_ids_and_normalized_name_collisions(self):
        with self.assertRaisesRegex(ValueError, "duplicate stable instrument ID"):
            fetch.local_import(**self.import_input(code=self.code))
        inputs = self.import_input()
        baseline = json.loads(self.baseline)
        baseline["symbols"]["  صندوق نمونه 2  "] = dict(self.old_entry, code="80003", history=daily_rows("80003"))
        self.history.write_text(json.dumps(baseline))
        with self.assertRaisesRegex(ValueError, "ambiguous display-name key"):
            fetch.local_import(**inputs)
        self.assertFalse(inputs["backup"].exists())

    def test_preview_rejects_stale_expected_history_hash(self):
        inputs = self.import_input()
        changed = self.baseline + b"\n"
        self.history.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "differs from expected"):
            fetch.local_import(**inputs, expected_history_sha256=fetch.watch.digest(self.baseline))
        self.assertEqual(self.history.read_bytes(), changed)
        self.assertFalse(inputs["backup"].exists())

    def test_cli_apply_fails_closed_even_with_valid_destination_hash(self):
        inputs = self.import_input()
        command = self.import_cli(inputs)
        approved_hash = fetch.watch.digest(self.baseline)
        for args in (["import", "--apply"], command + ["--apply"],
                     command + ["--apply", "--expected-history-sha256", approved_hash]):
            with self.subTest(args=args), patch.object(fetch, "local_import") as preview, \
                    patch.object(fetch.os, "open", side_effect=AssertionError("unexpected file access")), \
                    redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
                self.assertEqual(fetch.main(args), 2)
                preview.assert_not_called()
                self.assertIn("publication disabled: unresolved concurrent-writer safety", err.getvalue())
                self.assertFalse(out.getvalue())
        self.assertEqual(self.history.read_bytes(), self.baseline)
        self.assertFalse(inputs["backup"].exists())
        self.assertEqual(list(self.root.glob(".history-*.tmp")), [])
        self.opener.assert_not_called()

    def test_library_cannot_bypass_disabled_publication(self):
        inputs = self.import_input()
        with patch.object(fetch.os, "open", side_effect=AssertionError("unexpected file access")):
            with self.assertRaisesRegex(ValueError, "publication disabled: unresolved concurrent-writer safety"):
                fetch.local_import(**inputs, apply=True, expected_history_sha256=fetch.watch.digest(self.baseline))
        for name in ("publish_import", "create_exclusive", "write_synced"):
            self.assertFalse(hasattr(fetch, name))
        self.assertFalse(inputs["backup"].exists())
        self.assertEqual(self.history.read_bytes(), self.baseline)
        self.opener.assert_not_called()

    def test_paths_fail_closed_without_overwriting_any_backup(self):
        inputs = self.import_input()
        backup = inputs["backup"]
        link = self.root / "link-history"
        link.symlink_to(self.history)
        leaf = self.root / "link-dir"
        leaf.symlink_to(self.root, target_is_directory=True)
        real_fstat = os.fstat
        source_inode = inputs["source"].stat().st_ino

        def multiply_linked(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2) if info.st_ino == source_inode else info

        with patch.object(fetch.os, "fstat", side_effect=multiply_linked):
            with self.assertRaisesRegex(ValueError, "non-aliased"):
                fetch.local_import(**inputs)
        for changes in ({"backup": self.history}, {"backup": inputs["source"]},
                        {"history": inputs["source"]}, {"history": link},
                        {"backup": leaf / "backup-other"}, {"backup": self.stream / ".." / "other"},
                        {"backup": self.root / "missing-parent" / "other"}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, OSError)):
                fetch.local_import(**(inputs | changes))
        with self.assertRaisesRegex(ValueError, "protected archives"):
            fetch.local_import(**(inputs | {"backup": self.stream / "never-write"}))
        backup.symlink_to(self.root / "not-created")
        with self.assertRaisesRegex(ValueError, "backup already exists"):
            fetch.local_import(**inputs)
        self.assertTrue(backup.is_symlink())
        backup.unlink()
        backup.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "backup already exists"):
            fetch.local_import(**inputs)
        self.assertEqual(backup.read_bytes(), b"keep")
        self.assertEqual(self.history.read_bytes(), self.baseline)

    def csv_lines(self):
        first = date(2025, 12, 20)
        return [csv_row(first + timedelta(days=i), zero=i < 2, unusual=i == 1) for i in range(42)] + [
            csv_row(date(2026, 2, n)) for n in (1, 2)
        ]

    def csv_import_input(self, raw=None, received_at=None, code="80002"):
        if raw is None:
            raw = csv_export(self.csv_lines())
        return self.import_input(code, raw) | {
            "source_url": f"https://www.tsetmc.com/instInfo/{code}",
            "source_format": "tsetmc-csv", "received_at": received_at,
        }

    def csv_cli(self, inputs):
        args = self.import_cli(inputs)
        if inputs["received_at"] is None:
            start = args.index("--received-at")
            del args[start:start + 2]
        return args + ["--source-format", "tsetmc-csv"]

    def csv_entry(self, inputs):
        capture, bases = fetch.capture_inventory(inputs["capture_id"], stream=self.stream)
        return fetch.CSVObservation(
            inputs["code"], tuple(bases[inputs["code"]]), inputs["source"].read_bytes(),
            inputs["source_sha256"], inputs["source_url"], inputs["received_at"], capture,
        )

    def test_csv_hash_is_checked_before_decoding_or_dispatching_parser(self):
        inputs = self.csv_import_input(raw=b"\xffwrong")
        with patch.object(fetch, "validate_csv_history") as parser:
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                fetch.local_import(**(inputs | {"source_sha256": "0" * 64}))
        parser.assert_not_called()
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            fetch.validate_csv_history(b"\xff", "0" * 64, inputs["code"], received_at=None)
        self.assertEqual(self.history.read_bytes(), self.baseline)
        self.assertFalse(inputs["backup"].exists())

    def test_csv_columns_exact_cells_and_optional_blank_line(self):
        for blank in (False, True):
            with self.subTest(blank=blank):
                inputs = self.csv_import_input(raw=csv_export(self.csv_lines(), blank=blank))
                item = self.csv_entry(inputs).entry()
                rows = item["history"]
                self.assertEqual(len(rows), 44)
                self.assertEqual([r["dEven"] for r in rows], sorted((r["dEven"] for r in rows), reverse=True))
                self.assertEqual(item["provenance"]["ticker"], "Sample ETF")
                self.assertEqual(
                    tuple(rows[0][k] for k in ("priceFirst", "priceYesterday", "pClosing", "pDrCotVal")),
                    (99.125, 115, 100, 103),
                )
                self.assertEqual(tuple(rows[0]["_source_fields"]), fetch.CSV_HEADER)
                self.assertEqual(rows[0]["_source_fields"]["<FIRST>"], " 99.125")
                self.assertEqual(rows[0]["_source_fields"]["<OPENINT>"], "2")
                self.assertEqual(rows[0]["insCode"], "80002")
                self.assertNotIn("insCode", rows[0]["_source_fields"])
                self.assertEqual(rows[-2]["priceFirst"], 11690)
                self.assertEqual(rows[-2]["priceMax"], 0)
                self.assertFalse(rows[-2]["_quality"]["ohlc_valid"])
                self.assertTrue(rows[-2]["_quality"]["zero_volume_unusual_prices"])
                self.assertFalse(rows[-1]["_quality"]["volume_valid"])

    def test_csv_strict_syntax_values_and_active_ohlc(self):
        lines = self.csv_lines()
        base = lines[-1]
        cells = base.rstrip(b"\n").split(b",")
        invalid_fields = (
            (0, b"", "missing CSV field"), (0, b"Different", "ticker or period"),
            (0, b"\xff", "utf-8"), (1, b"20260230", "YYYYMMDD"),
            (1, b"2026-02-02", "YYYYMMDD"), (1, b"20260201", "duplicate"),
            (2, b"105", "inconsistent active"), (2, b"99.1250000000000000001", "precision"),
            (2, b"NaN", "invalid CSV number"), (2, b"Infinity", "invalid CSV number"),
            (3, b"9007199254740993", "exact numeric range"), (3, b"97", "inconsistent active"),
            (4, b"0", "inconsistent active"), (5, b"105", "inconsistent active"),
            (5, b"0", "invalid CSV closing"), (7, b"True", "invalid CSV number"),
            (7, b"-1", "invalid CSV number"), (7, b"0.1", "not an integer"),
            (9, b"W", "ticker or period"), (11, b"97", "inconsistent active"),
        )
        for pos, value, reason in invalid_fields:
            invalid = cells.copy()
            invalid[pos] = value
            raw = csv_export(lines[:-1] + [b",".join(invalid) + b"\n"])
            with self.subTest(pos=pos, value=value), self.assertRaisesRegex(ValueError, reason):
                self.csv_entry(self.csv_import_input(raw=raw)).entry()
        for raw, reason in (
            (csv_export(lines).replace(b"<FIRST>", b"<OPEN>", 1), "header"),
            (csv_export(lines).replace(b"\r\n\n", b"\r\n\n\n", 1), "invalid CSV row"),
            (csv_export(lines + [base.rstrip() + b",extra\n"]), "invalid CSV row"),
            (csv_export(lines[:-1] + [b",".join(cells[:-1]) + b"\n"]), "invalid CSV row"),
            (csv_export(lines + [b'"unterminated\n']), "CSV syntax"),
            (csv_export(lines + [lines[0]]), "duplicate"),
            (csv_export(lines[:22]), "fewer than 21"),
            (b" " * (fetch.MAX_CSV_BYTES + 1), "size limit"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                fetch.local_import(**self.csv_import_input(raw=raw))
        received = "2026-02-01T20:00:00+00:00"  # Still Feb 1 in Tehran.
        with self.assertRaisesRegex(ValueError, "future or duplicate"):
            fetch.local_import(**self.csv_import_input(received_at=received))

    def test_csv_dry_run_unknown_receipt_is_capture_bound_and_never_written(self):
        inputs = self.csv_import_input()
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        real_open = os.open

        def readonly_open(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            return real_open(path, flags, *args, **kwargs)

        with patch.object(fetch.os, "open", side_effect=readonly_open), \
                patch.object(fetch.os, "replace", side_effect=AssertionError("unexpected rename")), \
                redirect_stdout(StringIO()) as out:
            self.assertEqual(fetch.main(self.csv_cli(inputs)), 0)
        report = json.loads(out.getvalue())
        provenance = report["provenance"]
        self.assertEqual(report["mode"], "dry-run")
        self.assertIsNone(report["received_at"])
        self.assertEqual(provenance["receipt_status"], "UNKNOWN")
        self.assertEqual(provenance["capture_receipt_tehran"], "2026-02-01T11:30:00+03:30")
        self.assertEqual(provenance["instrument_page_url"], inputs["source_url"])
        self.assertIsNone(provenance["source_url"])
        self.assertIsNone(provenance["http_status"])
        self.assertFalse(provenance["ins_code_present_in_source"])
        self.assertEqual(provenance["identity_verification"], "operator_attributed_with_archival_corroboration")
        self.assertEqual(provenance["adjustment_status"], "UNKNOWN")
        self.assertEqual(provenance["body_sha256"], inputs["source_sha256"])
        self.assertIn("seal_sha256", provenance)
        self.assertEqual(report["csv_validation"]["session_date_cutoff_exclusive"], "2026-02-01")
        self.assertEqual(report["csv_validation"]["records_before_cutoff"], 42)
        self.assertEqual(report["csv_validation"]["excluded_at_or_after_cutoff"], 2)
        self.assertEqual(report["csv_validation"]["zero_volume_count"], 2)
        self.assertEqual(report["csv_validation"]["zero_volume_unusual_price_dates"], [20251221])
        coverage = report["capture_coverage"]
        self.assertEqual((coverage["before_available"], coverage["after_available"]), (1, 2))
        self.assertEqual((coverage["before_missing_ids"], coverage["after_missing_ids"]), (22, 21))
        self.assertEqual(coverage["instrument_after"]["valid_closes_before_cutoff"], 40)
        self.assertEqual(coverage["standard_research_reader"]["reason_code"], "history_evidence_incomplete")
        self.assertTrue(coverage["preview_only_without_download_receipt"])
        self.assertFalse(report["execution_eligible"])
        self.assertFalse(report["point_in_time_verified"])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        self.assertFalse(inputs["backup"].exists())
        self.opener.assert_not_called()

    def test_csv_identity_checks_and_receipt_policy(self):
        inputs = self.csv_import_input()
        for change, reason in (
            ({"source_url": "https://www.tsetmc.com/instInfo/80003"}, "source URL or instrument ID"),
            ({"capture_id": "manual:2"}, "absent from selected capture"),
            ({"received_at": "UNKNOWN"}, "receipt timestamp"),
            ({"received_at": "2026-02-02T00:00:00-00:00"}, "receipt timestamp"),
            ({"received_at": "2026-02-02T01:00:00+00:00"}, "future"),
            ({"source_format": "unknown"}, "source format"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, reason):
                fetch.local_import(**(inputs | change))
        capture, bases = fetch.capture_inventory("manual:1", stream=self.stream)
        for info, inventory, reason in (
            (capture | {"diagnostics": [{"excluded_from_options": True}]}, bases, "archive identity diagnostics"),
            (capture, dict(bases) | {"80002": {"alias", "conflicting"}}, "ambiguous display name"),
            (capture, dict(bases) | {"80003": {" صندوق نمونه 2 "}}, "ambiguous display name"),
        ):
            with self.subTest(reason=reason), patch.object(fetch, "capture_inventory", return_value=(info, inventory)), \
                    self.assertRaisesRegex(ValueError, reason):
                fetch.local_import(**inputs)
        with self.assertRaisesRegex(ValueError, "existing or duplicate"):
            fetch.local_import(**self.csv_import_input(code=self.code))

    def test_csv_merge_preserves_literal_khodro_and_no_apply_bypass(self):
        inputs = self.csv_import_input()
        observation = self.csv_entry(inputs)
        literal = json.dumps(self.old_entry, ensure_ascii=True).replace('"pClosing": 100', '"pClosing": 1.000e2')
        baseline = self.baseline.replace(json.dumps(self.old_entry, ensure_ascii=False).encode(), literal.encode())
        self.assertNotEqual(baseline, self.baseline)
        self.history.write_bytes(baseline)
        plan = fetch.prepare_merge(baseline, [observation], preview_unknown_csv_code=inputs["code"])
        self.assertIn(literal.encode(), plan.replacement)
        self.assertEqual(plan.baseline, self.history.read_bytes())
        document = json.loads(plan.replacement)
        self.assertEqual(document["metadata"], json.loads(baseline)["metadata"] | {"symbol_count": 2})
        with self.assertRaisesRegex(ValueError, "missing receipt"):
            fetch.history_ids(document)  # Not a normal consumable/publishable history.
        with patch.object(fetch.os, "open", side_effect=AssertionError("unexpected file access")), \
                redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
            self.assertEqual(fetch.main(self.csv_cli(inputs) + ["--apply"]), 2)
            self.assertIn("publication disabled", err.getvalue())
            self.assertFalse(out.getvalue())
            with self.assertRaisesRegex(ValueError, "publication disabled"):
                fetch.local_import(**inputs, apply=True, expected_history_sha256=fetch.watch.digest(baseline))
        self.assertEqual(self.history.read_bytes(), baseline)
        self.assertFalse(inputs["backup"].exists())

    def test_csv_full_market_exact_id_join_with_known_receipt_and_gates(self):
        inputs = self.csv_import_input(received_at=self.received.isoformat())
        imported = fetch.local_import(**inputs)
        self.assertFalse(imported["capture_coverage"]["preview_only_without_download_receipt"])
        self.assertEqual(imported["provenance"]["receipt_status"], "OPERATOR_SUPPLIED")
        observation = self.csv_entry(inputs)
        plan = fetch.prepare_merge(self.baseline, [observation])
        document = json.loads(plan.replacement)
        # An unrelated base using the CSV display key must not get the history.
        document["symbols"]["نام نمایشی دیگر"] = document["symbols"].pop(observation.key)
        index = {entry["code"]: [(name, entry)] for name, entry in document["symbols"].items()}
        sealed, _ = fetch.watch.read_object(self.stream / "00000001/body.raw.json", fetch.watch.MAX_BODY)
        rows, *_ = fetch.quality.inventory_contracts(sealed["instrumentOptMarketWatch"])
        # Exercise the real capture/ID join without running screening over the minimal fixture.
        states = {code: row | {
            "candidate_eligible": True, "candidate_rejection": None, "rank_eligible": False,
            "rank_rejection": fetch.quality.NO_MODEL, "iv_status": "ok",
            "greeks_assumption_status": "experimental_unverified",
        } for code, row in rows.items()}
        fake_index = (fetch.watch.digest(plan.replacement), document["metadata"], index, None)
        with patch.object(fetch.watch, "contract_states", return_value=states), \
                patch.object(fetch.combined, "history_index", return_value=fake_index), \
                patch.object(fetch.combined, "atomic_write", side_effect=AssertionError("unexpected write")):
            report = fetch.combined.build_market_research_report(
                "manual:1", stream=self.stream, legacy_archive=self.root, history_path=self.history
            )
        tech = report["underlyings"][inputs["code"]]["technical_analysis"]
        self.assertEqual((tech["status"], tech["symbol"]), ("AVAILABLE", "نام نمایشی دیگر"))
        self.assertEqual(report["underlyings"]["80003"]["technical_analysis"]["reason_code"], "history_id_not_found")
        self.assertEqual(tech["history"]["source"], "tsetmc_manual_csv")
        self.assertEqual(tech["history"]["provenance"]["body_sha256"], inputs["source_sha256"])
        self.assertEqual(tech["history"]["valid_close_count"], 40)
        self.assertEqual(tech["history"]["indicators"]["SMA(5)"]["value"], 100)
        self.assertTrue(tech["downloaded_after_capture"])
        self.assertEqual(report["summary"]["technical_available_count"], 2)
        self.assertEqual(report["summary"]["screening"]["strict_passes"], 0)
        self.assertTrue(report["research_only"])
        self.assertFalse(report["point_in_time_verified"])
        self.assertFalse(report["execution_eligible"])
        self.assertEqual(report["greeks_assumption_status"], "experimental_unverified")
        self.assertNotIn("BUY_READY", json.dumps(report))
        self.assertTrue(all(not row["execution_eligible"] for row in report["options"]))

    def test_csv_ahram_cutoff_regression_and_pinned_real_export_if_present(self):
        first = date(2021, 12, 20)
        lines = [csv_row(first + timedelta(days=i), zero=1 <= i <= 67 or i == 215, unusual=i == 215)
                 for i in range(1178)]
        synthetic = csv_export([csv_row(date(2026, 11, n)) for n in (23, 22, 21)] + lines[::-1])
        real = Path(__file__).with_name("data") / "ahram_tsetmc_20261124_0b7c2113.csv"
        exports = [(synthetic, fetch.watch.digest(synthetic))]
        if real.is_file():  # Ignored local source is not a dependency of the offline suite.
            exports.append((real.read_bytes(), AHRAM_SHA256))
        code = "17914401175772326"
        before = date(2026, 11, 22)
        for raw, sha in exports:
            with self.subTest(sha=sha):
                rows, _ = fetch.validate_csv_history(raw, sha, code, received_at=None)
                self.assertEqual(len(rows), 1181)
                self.assertEqual(sum(row["_quality"]["volume_valid"] for row in rows), 1113)
                unusual = [row["dEven"] for row in rows if row["_quality"]["zero_volume_unusual_prices"]]
                self.assertEqual(unusual, [20220723])
                item = {"code": code, "history": rows, "fetched_at": None, "provenance": {
                    "source_format": trends.CSV_SOURCE_FORMAT, "receipt_status": "UNKNOWN",
                    "capture_receipt_tehran": "2026-11-22T09:49:47.189635+03:30",
                }}
                with self.assertRaisesRegex(ValueError, "زمان محلی دریافت"):
                    trends.research_report("اهرم", item, {}, before=before)
                report = trends.research_report("اهرم", item, {}, before=date(2026, 12, 1),
                                                expected_code=code, preview_unknown_csv_receipt=True)
                prefix = item | {"history": [row for row in rows if row["dEven"] < 20261122]}
                prefix_report = trends.research_report("اهرم", prefix, {}, before=before,
                                                       preview_unknown_csv_receipt=True)
                self.assertEqual(report["session_date_cutoff_exclusive"], "2026-11-22")
                self.assertEqual(report["records_before_cutoff"], 1179)
                self.assertEqual(report["valid_close_count"], 1111)
                self.assertEqual(report["excluded_at_or_after_cutoff"], 2)
                self.assertEqual(len(report["zero_volume_excluded"]), 68)
                self.assertEqual(report["valid_close_date_range"], ["2021-12-20", "2026-11-21"])
                self.assertEqual(report["indicators"], prefix_report["indicators"])
                self.assertIsNone(report["downloaded_at_utc"])
                self.assertFalse(report["point_in_time_verified"])

    def test_csv_close_policy_uses_unmodified_rows_and_keeps_zero_volume(self):
        start = date(2026, 1, 1)
        lines = [csv_row(start + timedelta(days=i)) for i in range(25)]
        lines[0] = lines[0].replace(b" 100.00,", b" 110.25,", 1)
        lines[1] = lines[1].replace(b" 100.00,", b" 95.125,", 1)
        raw = csv_export(lines + [csv_row(date(2025, 12, 31), zero=True, unusual=True)])
        inputs = self.csv_import_input(raw=raw)
        default = self.csv_entry(inputs)
        with self.assertRaisesRegex(ValueError, "inconsistent active CSV OHLC/last"):
            default.entry()
        mode = fetch.CSV_CLOSE_POLICIES[1]
        entry = replace(default, close_policy=mode).entry()
        audit = entry["close_filter"]
        self.assertEqual(audit["flagged_row_count"], 2)
        self.assertEqual((audit["close_above_high_count"], audit["close_below_low_count"]), (1, 1))
        for record, line, day, reason in zip(audit["flagged_rows"][::-1], lines[:2], ("2026-01-01", "2026-01-02"),
                                             ("close_above_traded_high", "close_below_traded_low"), strict=True):
            self.assertEqual((record["session_date"], record["reason"]), (day, reason))
            self.assertEqual(list(record["source_fields"].values()), line.decode().rstrip("\n").split(","))
        self.assertEqual(entry["record_count"], 26)
        self.assertEqual(entry["quality"]["active_session_count"], 25)
        self.assertEqual(entry["quality"]["valid_ohlc_count"], 23)
        self.assertEqual(entry["quality"]["zero_volume_count"], 1)
        self.assertEqual({r["pClosing"] for r in entry["history"] if r["qTotTran5J"]}, {100})
        self.assertNotIn(20260101, {r["dEven"] for r in entry["history"]})
        self.assertNotIn(20260102, {r["dEven"] for r in entry["history"]})
        self.assertEqual(entry["history"][-1]["qTotTran5J"], 0)
        self.assertEqual(entry["history"][-1]["priceFirst"], 11690)
        self.assertEqual(inputs["source"].read_bytes(), raw)

    def test_csv_close_policy_does_not_relax_hash_ohlc_or_minimum(self):
        mode = fetch.CSV_CLOSE_POLICIES[1]
        base = [csv_row(date(2026, 1, 1) + timedelta(days=i)) for i in range(21)]
        for original, replacement, reason in (
            (b" 99.125,", b" 97.99,", "inconsistent active CSV OHLC/last"),
            (b",103.00\n", b",104.01\n", "inconsistent active CSV OHLC/last"),
            (b" 104.00,", b" 97.00,", "inconsistent active CSV OHLC/last"),
            (b" 100.00,", b" 110.00,", "fewer than 21 active sessions"),
            (b" 100.00,", b" 110.0000000000000001,", "decimal precision"),
        ):
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, reason):
                raw = csv_export([base[0].replace(original, replacement, 1), *base[1:]])
                fetch.validate_csv_history(raw, fetch.watch.digest(raw), "80002", received_at=None, close_policy=mode)
        with patch.object(fetch.csv, "reader", side_effect=AssertionError("parsed before verified hash")), \
                self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            fetch.validate_csv_history(b"\xffinvalid", "0" * 64, "80002", received_at=None, close_policy=mode)

    def test_pinned_shasta_source_conservative_counts_when_available(self):
        source = Path(__file__).with_name("data") / "shasta_tsetmc_20261124_ed1280c0.csv"
        if source.is_file():
            raw = source.read_bytes()
            sha = "ed1280c064b2e959e4eb9f72ccb6653b6469ba3f56e87ffa3a43aff4271b46f4"
            code = "2400322364771558"
            with self.assertRaisesRegex(ValueError, "inconsistent active CSV OHLC/last"):
                fetch.validate_csv_history(raw, sha, code, received_at=None)
            rows, _ = fetch.validate_csv_history(raw, sha, code, received_at=None,
                                                 close_policy=fetch.CSV_CLOSE_POLICIES[1])
            active = [r for r in rows if r["qTotTran5J"]]
            pre = [r for r in active if r["dEven"] < 20261122]
            flags = [r["_quality"]["close_anomaly"] for r in pre if "close_anomaly" in r["_quality"]]
            self.assertEqual((len(rows), len(active), len(pre)), (1589, 1386, 1384))
            self.assertEqual((flags.count("close_above_traded_high"), flags.count("close_below_traded_low")), (5, 17))
            self.assertEqual(sum(r["_quality"]["ohlc_valid"] for r in pre), 1362)

if __name__ == "__main__":
    unittest.main()
