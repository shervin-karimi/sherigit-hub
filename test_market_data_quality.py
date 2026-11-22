"""Offline tests against raw quotes and sealed temporary observations."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import market_data_quality as audit
import shadow_collector as collector
import shadow_watch as watch


def row(**overrides):
    return {
        "insCode_C": "123", "insCode_P": "124", "uaInsCode": "456", "lval30_UA": "نمونه",
        "strikePrice": 100, "contractSize": 1000, "endDate": "20270127", "remainedDay": 67,
        "pDrCotVal_UA": 101, "pClosing_UA": 102,
        **{f"{key}_{side}": value for side in "CP" for key, value in
           (("pMeDem", 40), ("pMeOf", 41), ("qTitMeDem", 3), ("qTitMeOf", 4),
            ("zTotTran", 50), ("qTotTran5J", 100))},
        **overrides,
    }


class MarketDataQualityTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stream = Path(directory.name) / "manual"
        self.stream.mkdir()
        for name in ("socket.socket", "socket.getaddrinfo", "sqlite3.connect", "shadow_collector.fetch"):
            guard = patch(name, side_effect=AssertionError(f"offline audit called {name}"))
            guard.start()
            self.addCleanup(guard.stop)

    def append(self, day, **overrides):
        at = datetime.fromisoformat(f"2026-11-{day}T09:00:00+00:00")
        raw = collector.json_bytes({"instrumentOptMarketWatch": [row(**overrides)]})
        with collector.locked(self.stream, exclusive=True) as fd:
            return collector.append(self.stream, fd, collector.read_stream(self.stream),
                                    raw, at - timedelta(seconds=1), at, {})

    def test_quote_classes_and_locked_spread(self):
        cases = [
            ({}, "two_sided"),
            (dict(pMeDem_C=41), "two_sided"),
            (dict(pMeDem_C=42), "crossed"),
            (dict(pMeOf_C=0, qTitMeOf_C=0), "bid_only"),
            (dict(pMeDem_C=None, qTitMeDem_C=None), "ask_only"),
            (dict(pMeDem_C=0, qTitMeDem_C=0, pMeOf_C=None, qTitMeOf_C=None), "missing"),
            (dict(qTitMeDem_C=0), "inconsistent"),
            (dict(pMeDem_C=None), "inconsistent"),
            (dict(qTitMeDem_C=-1), "inconsistent"),
            (dict(pMeDem_C=True), "inconsistent"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(audit.inspect_contract(row(**overrides), "C")["quote_class"], expected)
        locked = audit.inspect_contract(row(pMeDem_C=41), "C")
        self.assertTrue(locked["locked_prices"])
        self.assertEqual(locked["spread_percent"], 0)
        self.assertIsNone(audit.inspect_contract(row(pMeDem_C=42), "C")["spread_percent"])

    def test_liquidity_and_underlying_are_separate_observations(self):
        report = audit.inspect_contract(row(zTotTran_C=0, pClosing_UA=0, pDrCotVal_UA=-1), "C")
        self.assertEqual(report["quote_class"], "two_sided")
        self.assertEqual(report["liquidity_issues"], ["trade_count_below_minimum_or_unusable"])
        self.assertTrue(report["trade_counter_inconsistent"])
        self.assertEqual(report["field_status"]["underlying_last_price"], "invalid")
        self.assertEqual(report["field_status"]["underlying_closing_price"], "zero")
        self.assertEqual(report["underlying_reference_status"], "missing_or_invalid_reference")
        valid = audit.inspect_contract(row(), "C")
        self.assertEqual(valid["underlying_reference_status"], "present_unverified_synchrony_unknown")

    def test_identity_duplicate_invalid_id_and_expiry_rejected(self):
        self.assertEqual(audit.raw_contracts([row(insCode_C="0")]).keys(), {"124"})
        for records, problem in (([row(), row()], "duplicate instrument ID"),
                                 ([row(insCode_C=True)], "invalid option or underlying ID"),
                                 ([row(endDate="20270230")], "invalid expiry")):
            with self.subTest(problem=problem), self.assertRaisesRegex(watch.ArchiveError, problem):
                audit.raw_contracts(records)

    def test_numeric_anomaly_is_reported_without_fabricating_screening(self):
        raw = collector.json_bytes({"instrumentOptMarketWatch": [row(pClosing_UA=-1)]})
        path = self.stream.parent / "fixture.json"
        path.write_bytes(raw)
        meta = {"path": path, "body_sha256": watch.digest(raw), "rows": 1, "byte_count": len(raw),
                "sequence": 1, "head_sha256": "fixture", "receipt_tehran": None, "receipt_utc": None,
                **dict.fromkeys(audit.TIME_FIELDS)}
        with patch.object(watch, "contract_states", side_effect=AssertionError("must not run scanner")):
            result = audit.read_capture(meta)
        self.assertEqual(result["summary"]["missing_or_invalid_underlying_reference"], 2)
        self.assertIsNone(result["summary"]["screening"])

    def test_compare_aligns_ids_not_symbols_and_never_differences_counters(self):
        left = audit.raw_contracts([row(lVal18AFC_C="old")])
        right = audit.raw_contracts([row(lVal18AFC_C="new", pMeOf_C=43, zTotTran_C=5, insCode_P="125")])
        comparison = audit.compare(left, right)
        self.assertEqual(comparison["common_same_identity"], 1)
        self.assertEqual(comparison["appeared_ids"], ["125"])
        self.assertEqual(comparison["disappeared_ids"], ["124"])
        self.assertEqual(comparison["two_sided_at_both_changes"], {"bid_or_ask": 1, "spread": 1, "depth": 0})
        self.assertFalse(comparison["trade_counter_deltas_reported"])
        self.assertFalse(comparison["equivalent_full_day_activity"])
        right["123"]["contract_size"] += 1
        with self.assertRaisesRegex(watch.ArchiveError, "identity changed: 123"):
            audit.compare(left, right)

    def test_pinned_real_shape_reports_reproducibly_and_never_promotes(self):
        self.append(21)
        self.append(22, pMeOf_C=42)
        snapshot = lambda: {p: p.read_bytes() for p in self.stream.glob("*/*.json")}
        before = snapshot()
        data = audit.build_report(self.stream)
        self.assertEqual(before, snapshot())
        self.assertEqual(data, audit.build_report(self.stream))
        self.assertFalse(data["execution_eligible"])
        self.assertEqual(data["greeks_assumption_status"], "experimental_unverified")
        for obs, day in zip(data["observations"], (21, 22)):
            self.assertEqual(obs["receipt_tehran"], f"2026-11-{day}T12:30:00+03:30")
            self.assertTrue(all(obs[k] is None for k in audit.TIME_FIELDS))
            self.assertEqual(obs["summary"]["quote_provenance_blocked"], 2)
            self.assertEqual(obs["summary"]["screening"]["passes_existing_screen"], 2)
            self.assertEqual(obs["summary"]["screening"]["candidate_model_unverified"], 2)
            self.assertEqual(obs["summary"]["screening"]["rank_eligible"], 0)
            self.assertEqual(obs["by_type"]["CALL"]["contracts"], 1)
            self.assertEqual(obs["by_underlying_id"]["456"]["symbols"], ["نمونه"])
        self.assertIn("تفاضل آن‌ها", audit.markdown(data))

    def test_missing_capture_and_body_tampering_fail_closed(self):
        self.append(21)
        with self.assertRaisesRegex(watch.ArchiveError, "missing pinned capture manual:2"):
            audit.build_report(self.stream)
        self.append(22, pMeOf_C=42)
        second = self.stream / "00000002" / "body.raw.json"
        second.chmod(0o600)
        second.write_bytes(b"{}")
        with self.assertRaisesRegex(watch.ArchiveError, "SHA-256 mismatch"):
            audit.build_report(self.stream)
