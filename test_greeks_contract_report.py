"""Offline fixtures for receipt selection, evidence and model summaries."""

from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import greeks_contract_report as report
import shadow_collector as collector
import shadow_watch as watch


class ContractReportTests(unittest.TestCase):
    code = "60210342490439455"
    receipt = "2026-11-21T13:19:57+03:30"

    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.stream = self.root / "manual"
        self.stream.mkdir()
        for name in ("socket.socket", "sqlite3.connect", "shadow_collector.fetch"):
            guard = patch(name, side_effect=AssertionError(f"offline test called {name}"))
            guard.start()
            self.addCleanup(guard.stop)

    def append(self, hour, **overrides):
        at = datetime.fromisoformat(f"2026-11-21T{hour:02d}:00:00+00:00")
        row = {
            "uaInsCode": "65883838195688438", "insCode_C": self.code, "insCode_P": "0",
            "lVal18AFC_C": "ضخود1165", "lVal30_C": "اختيارخ خودرو-650-1405/11/07", "lval30_UA": "خودرو",
            "strikePrice": 650, "contractSize": 1000, "endDate": "20270127", "remainedDay": 67,
            "pClosing_UA": 759, "pDrCotVal_UA": 765, "pMeDem_C": 185 + hour, "pMeOf_C": 186 + hour,
            "qTitMeDem_C": 10, "qTitMeOf_C": 9, "zTotTran_C": 300 - hour, "qTotTran5J_C": 200,
            **overrides,
        }
        raw = collector.json_bytes({"instrumentOptMarketWatch": [row]})
        with collector.locked(self.stream, exclusive=True) as fd:
            prior = collector.read_stream(self.stream)
            return collector.append(self.stream, fd, prior, raw, at - timedelta(seconds=1), at, {})

    def build(self, through=None):
        # Ignore the local legacy archive; the manual chain is real on disk but test-only.
        with patch.object(watch, "records_in_order", return_value=[]):
            return report.build_report(self.root / "unused", self.stream, through)

    def model_row(self, **updates):
        return dict(maturity_date="20270127", remaining_days=90, underlying_last_price=100,
                    strike_price=100, option_type="CALL", best_bid=8, best_ask=8) | updates

    def test_latest_receipts_not_latest_distinct_dates_and_pin(self):
        self.append(7)
        self.append(8)
        pinned = self.build("manual:2")
        self.append(9)
        self.assertEqual(pinned, self.build("manual:2"))
        obs = self.build()["observations"]
        self.assertEqual([o["capture_id"] for o in obs], ["manual:2", "manual:3"])
        self.assertEqual([o["receipt_tehran"][11:16] for o in obs], ["11:30", "12:30"])
        with self.assertRaisesRegex(watch.ArchiveError, "unknown --through"):
            self.build("manual:4")

    def test_evidence_and_counters_do_not_upgrade_archive_or_model(self):
        self.append(7)
        self.append(8)
        data = self.build()
        self.assertFalse(data["daily_trade_activity_comparable"])
        self.assertFalse(data["counter_deltas_reported"])
        self.assertFalse(data["execution_eligible"])
        self.assertIsNone(data["secondary_lead"]["retrieved_at"])
        self.assertEqual(data["evidence"]["settlement"]["status"], "secondary_lead_only")
        self.assertEqual(data["greeks_assumption_status"], "experimental_unverified")
        registry = data["evidence_registry"]
        self.assertEqual(registry["registry_version"], 1)
        self.assertFalse(registry["execution_eligible"])
        self.assertEqual(registry["greeks_assumption_status"], "experimental_unverified")
        self.assertEqual(registry["topics"]["quote_timestamps"]["status"], "missing_primary")
        self.assertEqual(data["scenarios"][0]["annual_continuous_rate"], 0.4394)
        for obs, counter in zip(data["observations"], (293, 292)):
            row = obs["targets"][self.code]
            self.assertEqual(row["trade_count"], counter)
            self.assertEqual(row["secondary_lead_match"], "archive_family_match")
            self.assertEqual(obs["market_wide"]["rank_eligible_count"], 0)
            self.assertIsNone(obs["exchange_quote_timestamp"])
            self.assertEqual(obs["targets"]["13308854503358443"]["status"], "absent")

    def test_fixed_mid_refit_and_fixed_vol_price_remain_distinct(self):
        ev = report.evaluate(self.model_row(), self.receipt)
        self.assertEqual(ev["calendar_days"], 67)  # never substitutes remaining_days=90
        self.assertTrue(ev["roundtrip"]["pass"])
        by_key = {s["id"]: s for s in ev["scenarios"]}
        base, low, high = (by_key[key] for key in ("baseline", "rate_low", "rate_high"))
        self.assertAlmostEqual(low["refit_price"], 8, places=8)
        self.assertGreater(low["iv"], base["iv"])
        self.assertLess(low["fixed_baseline_iv_price"], 8)
        self.assertEqual(high["iv_failure_reason"], "below_theoretical_bound")
        for field in ("iv", "delta", "theta_per_day", "refit_price"):
            self.assertIsNone(high[field])
        self.assertGreater(high["fixed_baseline_iv_price"], 8)

    def test_invalid_quotes_spot_and_expiry_are_not_refit(self):
        for changes, expected in (
            ({"best_bid": 9}, "unusable_bid_ask"),
            ({"underlying_last_price": 0}, "missing_positive_spot"),
            ({"maturity_date": "20261121"}, "nonpositive_calendar_time"),
        ):
            with self.subTest(reason=expected):
                ev = report.evaluate(self.model_row(**changes), self.receipt)
                self.assertEqual(ev["input_failure"], expected)
                self.assertEqual(ev["scenarios"], [])
                self.assertIsNone(ev["roundtrip"])

    def test_market_paired_denominators_are_not_solved_totals(self):
        rows = {"x": self.model_row(), "y": self.model_row(best_bid=0)}
        states = {key: dict(two_sided=False, candidate_eligible=False, rank_eligible=False, rank_rejection=None)
                  for key in rows}
        m = report.summarize(states, {k: report.evaluate(r, self.receipt) for k, r in rows.items()})
        self.assertEqual(m["model_inputs_usable"], 1)
        self.assertEqual(m["roundtrips_checked"], 1)
        self.assertEqual(m["roundtrips_skipped"], 1)
        high = next(s for s in m["scenarios"] if s["id"] == "rate_high")
        self.assertEqual((high["iv_solved"], high["paired_refits"], high["fixed_iv_prices_compared"]), (0, 0, 1))
        self.assertIsNone(high["median_iv_change_pp"])

    def test_changed_identity_or_corrupt_body_is_rejected(self):
        self.append(7)
        self.append(8, contractSize=999)
        with self.assertRaisesRegex(watch.ArchiveError, "identity changed"):
            self.build()
        body = self.stream / "00000002" / "body.raw.json"
        body.chmod(0o600)
        body.write_bytes(b"{}")
        with self.assertRaisesRegex(watch.ArchiveError, "SHA-256 mismatch"):
            self.build()

    def test_cli_writes_two_repeatable_outputs(self):
        self.append(7)
        self.append(8)
        out = self.root / "reports"
        before = {p: p.read_bytes() for p in self.stream.glob("*/*.json")}
        args = ["--legacy", str(self.root / "unused"), "--stream", str(self.stream)]
        with patch.object(watch, "records_in_order", return_value=[]), patch.object(report, "OUTPUT", out):
            self.assertEqual(report.main(args), 0)
            outputs = {p.name: p.read_bytes() for p in out.iterdir()}
            self.assertEqual(report.main(args), 0)
        self.assertEqual(outputs, {p.name: p.read_bytes() for p in out.iterdir()})
        self.assertEqual(before, {p: p.read_bytes() for p in self.stream.glob("*/*.json")})
        self.assertEqual(set(outputs), {"greeks-contract-validation.json", "greeks-contract-validation.fa.md"})
        self.assertEqual(json.loads(outputs["greeks-contract-validation.json"])["schema_version"], 1)
        self.assertIn("زمان دریافت تهران (نه زمان مظنه)", outputs["greeks-contract-validation.fa.md"].decode())
