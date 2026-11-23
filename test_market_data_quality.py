"""Offline tests against raw quotes and sealed temporary observations."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import analyze_underlying_trends_v2 as trends
import combine_analysis as combine
import greeks_evidence_registry as evidence
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
        return self.append_rows(day, [row(**overrides)])

    def append_rows(self, day, rows, *, hour=9, stream=None):
        stream = self.stream if stream is None else stream
        at = datetime(2026, 11, day, hour, tzinfo=timezone.utc)
        raw = collector.json_bytes({"instrumentOptMarketWatch": rows})
        with collector.locked(stream, exclusive=True) as fd:
            return collector.append(stream, fd, collector.read_stream(stream),
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

    def history_item(self):
        code = combine.RESEARCH_UNDERLYING
        return {"code": code, "fetched_at": "2026-11-25T07:00:00+03:30", "history": [
            {"insCode": code, "dEven": 20261100 + day, "pClosing": close, "qTotTran5J": 100,
             "priceMin": close - 1, "priceMax": close + 1}
            for day, close in zip((17, 18, 19, 20, 21, 22, 23), (101, 102, 103, 104, 105, 900, 990))
        ]}

    def history_file(self, symbols):
        path = self.stream.parent / "history.json"
        path.write_text(json.dumps({"metadata": {"source": "synthetic fixture"}, "symbols": symbols}), encoding="utf-8")
        return path

    def research_cli(self, history, *, stream=None, options=()):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = combine.research_main([
                "--research", "--capture", "manual:2", "--underlying-code", combine.RESEARCH_UNDERLYING,
                "--stream", str(stream or self.stream), "--history", str(history), *options,
            ])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_research_cli_pins_capture_joins_ids_and_never_writes(self):
        code = combine.RESEARCH_UNDERLYING
        self.append(20)
        receipt = self.append_rows(21, [
            row(uaInsCode=code, lval30_UA="نام تازه در آرشیو"),
            row(insCode_C="125", insCode_P="126", uaInsCode="456", lval30_UA="نام تاریخچه"),
        ], hour=21)  # Nov 22 Tehran even though it is still Nov 21 UTC.
        self.append(23, uaInsCode=code, pMeOf_C=80)
        history = self.history_file({"نام تاریخچه": self.history_item()})
        snapshot = lambda: {p: p.read_bytes() for p in history.parent.rglob("*") if p.is_file()}
        before = snapshot()
        with patch.object(audit, "read_capture", wraps=audit.read_capture) as replay, \
                patch.object(combine, "atomic_write", side_effect=AssertionError("unexpected legacy write")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP")), \
                patch("paper_trading.PaperTrading.buy", side_effect=AssertionError("unexpected order")), \
                patch("paper_trading.PaperTrading.sell", side_effect=AssertionError("unexpected order")), \
                patch("approval_workflow.ApprovalStore.decide", side_effect=AssertionError("unexpected approval")):
            status, stdout, errors = self.research_cli(history)
            self.assertEqual((status, errors), (0, ""))
            self.assertEqual(self.research_cli(history), (status, stdout, errors))
            self.assertEqual([call.args[0]["sequence"] for call in replay.call_args_list], [2, 2])
        self.assertEqual(before, snapshot())
        report = json.loads(stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["research_only"])
        self.assertFalse(report["execution_eligible"])
        self.assertFalse(report["point_in_time_verified"])
        self.assertEqual(report["capture"]["capture_id"], "manual:2")
        self.assertEqual(report["capture"]["body_sha256"], receipt["body_sha256"])
        self.assertEqual(report["capture"]["seal_sha256"], receipt["head_sha256"])
        self.assertEqual(report["capture"]["quote_freshness"], "UNKNOWN")
        self.assertFalse(report["capture"]["spot_quote_synchrony_verified"])
        self.assertTrue(all(report["capture"][key] is None for key in audit.TIME_FIELDS))
        self.assertEqual(report["capture_date_cutoff_exclusive"], "2026-11-22")
        underlying = report["underlying"]
        self.assertEqual((underlying["ins_code"], underlying["symbol"]), (code, "نام تاریخچه"))
        self.assertTrue(underlying["downloaded_after_capture"])
        self.assertEqual(underlying["history"]["valid_close_date_range"][-1], "2026-11-21")
        self.assertEqual(underlying["history"]["excluded_at_or_after_cutoff"], 2)
        self.assertEqual(underlying["history"]["indicators"]["SMA(5)"]["value"], 103)
        self.assertEqual(report["greeks_assumption_status"], "experimental_unverified")
        self.assertEqual(report["risk"], {"status": "UNKNOWN", "max_quantity": None})
        self.assertEqual(report["option_counts"], {"CALL": 1, "PUT": 1})
        self.assertEqual([(o["ins_code"], o["option_type"]) for o in report["options"]], [("123", "CALL"), ("124", "PUT")])
        for option in report["options"]:
            self.assertFalse(option["execution_eligible"])
            self.assertEqual(option["status"], "RESEARCH_ONLY")
            self.assertEqual(option["reason_code"], "descriptive_comparison")
            self.assertEqual(option["identity_status"], "OBSERVED_UNVERIFIED")
            self.assertEqual(option["price_unit"], "UNKNOWN")
            self.assertEqual(option["trade_cutoff_status"], "UNKNOWN")
            self.assertEqual(option["settlement_status"], "UNKNOWN")
            self.assertTrue(option["screening"]["candidate_eligible"])
            self.assertFalse(option["screening"]["rank_eligible"])
            self.assertEqual(option["screening"]["greeks_assumption_status"], "experimental_unverified")

    def test_research_classifies_screen_rejection_and_missing_quote_separately(self):
        self.append(21)
        self.append(22, uaInsCode=combine.RESEARCH_UNDERLYING, pMeOf_C=80, pMeDem_P=None, qTitMeDem_P=None)
        history = self.history_file({"نمونه": self.history_item()})
        status, stdout, errors = self.research_cli(history)
        self.assertEqual((status, errors), (0, ""))
        call, put = json.loads(stdout)["options"]
        self.assertEqual((call["status"], call["reason_code"]), ("REJECTED", "wide_spread"))
        self.assertEqual((put["status"], put["reason_code"]), ("UNKNOWN", "screening_input_missing"))
        self.assertEqual(put["screening"]["candidate_rejection"], "invalid_quote_or_closing_spot")
        self.assertTrue(all(o["reason_fa"] and not o["screening"]["candidate_eligible"] for o in (call, put)))

    def test_research_history_fails_closed_without_symbol_fallback(self):
        self.append(21)
        self.append(22, uaInsCode=combine.RESEARCH_UNDERLYING)
        item = self.history_item()
        conflicting = item | {"history": [item["history"][0] | {"insCode": "789"}, *item["history"][1:]]}
        for symbols, expected, reason in (
                (None, "UNKNOWN", "history_file_missing"),
                ({"نمونه": item | {"code": None}}, "UNKNOWN", "history_id_not_found"),
                ({"نمونه": item | {"code": "789"}}, "UNKNOWN", "history_id_not_found"),
                ({"نمونه": item, "نام دیگر": item}, "UNKNOWN", "history_id_ambiguous"),
                ({"نمونه": item | {"history": None}}, "UNKNOWN", "history_evidence_incomplete"),
                ({"نمونه": item | {"history": item["history"][:1]}}, "UNKNOWN", "insufficient_history"),
                ({"نمونه": conflicting}, "REJECTED", "history_records_invalid"),
        ):
            with self.subTest(reason=reason):
                path = self.history_file(symbols) if symbols is not None else self.stream.parent / "absent.json"
                report = combine.build_research_report("manual:2", combine.RESEARCH_UNDERLYING,
                                                       stream=self.stream, history_path=path)
                self.assertEqual((report["underlying"]["status"], report["underlying"]["reason_code"]), (expected, reason))
                self.assertEqual({o["status"] for o in report["options"]}, {expected})
                self.assertFalse(report["execution_eligible"])
                self.assertIsNone(report["risk"]["max_quantity"])

    def test_research_refuses_duplicate_or_conflicting_option_ids_in_sealed_body(self):
        base = combine.RESEARCH_UNDERLYING
        first = row(uaInsCode=base)
        for other in (first, row(insCode_C="999", insCode_P="123", uaInsCode="789",
                                 strikePrice=110, endDate="20270227", contractSize=2000)):
            with self.subTest(other=other), TemporaryDirectory() as scratch:
                stream = Path(scratch)
                self.append_rows(21, [row()], stream=stream)
                # Seal intentionally invalid contents to exercise validation on reading.
                with patch.object(watch, "contract_states"):
                    self.append_rows(22, [first, other], stream=stream)
                status, stdout, errors = self.research_cli(stream / "absent.json", stream=stream)
                self.assertEqual((status, stdout), (1, ""))
                self.assertIn("duplicate instrument ID: 123", errors)

    def test_research_refuses_missing_archive_base_id_even_with_matching_name(self):
        self.append(21)
        with patch.object(watch, "contract_states"):
            self.append(22, uaInsCode=None, lval30_UA="خودرو")
        status, stdout, errors = self.research_cli(self.history_file({"خودرو": self.history_item()}))
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("invalid option or underlying ID", errors)

    def test_research_refuses_raw_and_replayed_identity_disagreement(self):
        self.append(21)
        self.append(22, uaInsCode=combine.RESEARCH_UNDERLYING)
        real_replay = watch.contract_states

        def conflicting_replay(path, meta):
            states = real_replay(path, meta)
            states["123"]["contract_size"] += 1
            return states

        with patch.object(watch, "contract_states", side_effect=conflicting_replay):
            status, stdout, errors = self.research_cli(self.history_file({"خودرو": self.history_item()}))
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("raw and scanner instrument identities disagree", errors)

    def test_research_missing_modified_or_corrupt_archive_fails_without_fallback(self):
        history = self.stream.parent / "absent.json"
        self.append(21)
        missing = self.research_cli(history)
        self.assertEqual(missing[:2], (1, ""))
        self.assertIn("manual:2", missing[2])
        self.append(22, uaInsCode=combine.RESEARCH_UNDERLYING)
        target = self.stream / "00000002"
        for name, error in (("seal.json", "seal.json"), ("body.raw.json", "SHA-256 mismatch")):
            path = target / name
            original = path.read_bytes()
            path.chmod(0o600)
            path.write_bytes(b"{")
            status, stdout, errors = self.research_cli(history)
            self.assertEqual((status, stdout), (1, ""))
            self.assertIn(error, errors)
            path.write_bytes(original)

    def market_cli(self, history, *, capture="manual:2", options=()):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = combine.research_main([
                "--research", "--capture", capture, "--all-underlyings",
                "--stream", str(self.stream), "--history", str(history), *options,
            ])
        return result, out.getvalue(), err.getvalue()

    def test_market_cli_discovers_twenty_bases_and_replays_selected_capture_once_per_call(self):
        code = combine.RESEARCH_UNDERLYING
        bases = [code, *(str(500 + i) for i in range(19))]
        rows = [row(insCode_C=str(1000 + i * 2), insCode_P=str(1001 + i * 2), uaInsCode=base,
                    lval30_UA="نام مشترک") for i, base in enumerate(bases)]
        rows[0]["pMeOf_P"] = 80
        rows[1].update(pMeDem_C=None, qTitMeDem_C=None)
        self.append(20)
        receipt = self.append_rows(21, rows, hour=21)
        self.append(23, uaInsCode="999")
        history = self.history_file({"نام قدیمی": self.history_item(),
                                     "نام مشترک": self.history_item() | {"code": "990"}})
        snapshot = lambda: {p: p.read_bytes() for p in history.parent.rglob("*") if p.is_file()}
        before = snapshot()
        with patch.object(watch, "contract_states", wraps=watch.contract_states) as replay, \
                patch.object(combine, "history_index", wraps=combine.history_index) as index, \
                patch.object(trends, "research_report", wraps=trends.research_report) as technical, \
                patch.object(evidence, "current_assessment", wraps=evidence.current_assessment) as registry, \
                patch.object(combine, "atomic_write", side_effect=AssertionError("unexpected persistent write")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP")), \
                patch("paper_trading.PaperTrading.buy", side_effect=AssertionError("unexpected order")), \
                patch("paper_trading.PaperTrading.sell", side_effect=AssertionError("unexpected order")), \
                patch("approval_workflow.ApprovalStore.decide", side_effect=AssertionError("unexpected approval")):
            result = self.market_cli(history, options=("--format", "json"))
            self.assertEqual((result[0], result[2]), (0, ""))
            for mocked in (replay, index, technical, registry):
                self.assertEqual(mocked.call_count, 1)
            self.assertEqual(self.market_cli(history, options=("--format", "json")), result)
            summary_status, summary_text, summary_errors = self.market_cli(history)
            self.assertEqual((summary_status, summary_errors), (0, ""))
            self.assertEqual([call.args[1]["sequence"] for call in replay.call_args_list], [2, 2, 2])
        self.assertEqual(before, snapshot())
        self.assertLess(len(summary_text.encode()), 1500)
        self.assertNotIn("نام مشترک", summary_text)
        self.assertIn("اختیار یکتا: 40", summary_text)
        report = json.loads(result[1])
        self.assertEqual(report["schema_version"], 2)
        self.assertTrue(report["research_only"])
        self.assertFalse(report["execution_eligible"])
        self.assertFalse(report["point_in_time_verified"])
        self.assertEqual(report["risk"], {"status": "UNKNOWN", "max_quantity": None})
        capture = report["capture"]
        self.assertEqual(capture["capture_id"], "manual:2")
        self.assertEqual(capture["body_sha256"], receipt["body_sha256"])
        self.assertEqual(capture["seal_sha256"], receipt["head_sha256"])
        self.assertIsNone(capture["manifest_sha256"])
        self.assertTrue(all(capture[key] is None for key in audit.TIME_FIELDS))
        self.assertFalse(capture["spot_quote_synchrony_verified"])
        self.assertEqual(capture["quote_freshness"], "UNKNOWN")
        self.assertEqual(report["capture_date_cutoff_exclusive"], "2026-11-22")
        self.assertEqual(set(report["underlyings"]), set(bases))
        tech = report["underlyings"][code]["technical_analysis"]
        self.assertEqual((tech["status"], tech["symbol"]), ("AVAILABLE", "نام قدیمی"))
        self.assertTrue(tech["downloaded_after_capture"])
        self.assertEqual(tech["history"]["excluded_at_or_after_cutoff"], 2)
        self.assertEqual(tech["history"]["valid_close_date_range"][-1], "2026-11-21")
        self.assertEqual(tech["history"]["indicators"]["SMA(5)"]["value"], 103)
        for base in bases[1:]:
            tech = report["underlyings"][base]["technical_analysis"]
            self.assertEqual((tech["status"], tech["reason_code"]), ("NOT_AVAILABLE", "history_id_not_found"))
            self.assertIsNone(tech["history"])
        self.assertEqual(report["greeks_assumption_status"], "experimental_unverified")
        summary = report["summary"]
        self.assertEqual(summary["option_counts"], {"CALL": 20, "PUT": 20})
        self.assertEqual(summary["technical_available_count"], 1)
        self.assertEqual(summary["research_counts"], {"RESEARCH_ONLY": 1, "UNKNOWN": 38, "REJECTED": 1})
        self.assertEqual(summary["screening"], {"evaluated": True, "candidate_passes": 38,
                                                 "candidate_passes_by_type": {"CALL": 19, "PUT": 19}, "strict_passes": 0})
        self.assertEqual(report["reconciliation"]["identified_unique_contracts"], 40)
        self.assertEqual(report["diagnostics"], [])
        ids = [opt["ins_code"] for opt in report["options"]]
        self.assertEqual(ids, [str(i) for i in range(1000, 1040)])
        options = {o["ins_code"]: o for o in report["options"]}
        self.assertEqual(options["1001"]["screening"]["candidate_rejection"], "wide_spread")
        self.assertEqual(options["1002"]["reason_code"], "screening_input_missing")
        self.assertEqual(options["1003"]["status"], "UNKNOWN")
        self.assertTrue(options["1003"]["screening"]["candidate_eligible"])
        self.assertFalse(options["1003"]["screening"]["rank_eligible"])
        self.assertEqual(options["1003"]["screening"]["rank_rejection"], audit.NO_MODEL)
        self.assertTrue(all(o["reason_fa"] and not o["execution_eligible"] and "score" not in o for o in options.values()))

    def test_market_conflicting_and_malformed_sides_reconcile_and_block_replay(self):
        self.append(21)
        body = [
            row(insCode_P="0", **{f"{name}_P": 0 for name in audit.SIDE_FIELDS.values()}),
            row(insCode_P="125"),
            row(insCode_C="126", insCode_P="126", uaInsCode="999"),
            row(insCode_C=None, insCode_P="127", uaInsCode="700"),
            row(insCode_C=True, insCode_P="128", uaInsCode="701"),
            row(insCode_C="129", insCode_P="130", endDate="20270230"),
            row(insCode_C="131", insCode_P="132", qTitMeDem_C=-1),
            row(insCode_C="133", insCode_P="134", uaInsCode=None),
        ]
        with patch.object(watch, "contract_states"):  # Fixture only; deliberately seal defective records.
            self.append_rows(22, body)
        with patch.object(watch, "contract_states", side_effect=AssertionError("must not replay invalid identity")):
            status, stdout, errors = self.market_cli(self.stream.parent / "absent.json", options=("--format", "json"))
        self.assertEqual((status, errors), (0, ""))
        report = json.loads(stdout)
        counts = report["reconciliation"]
        self.assertEqual(counts, {
            "raw_paired_rows": 8, "raw_sides": 16, "empty_sides": 1, "diagnostic_only_sides": 10,
            "identified_unique_contracts": 5,
            "diagnostics_by_reason": {"duplicate_option_id": 2, "conflicting_option_id": 2,
                                      "missing_option_id": 1, "invalid_identifier": 1, "invalid_expiry": 2,
                                      "invalid_numeric_fields": 1, "missing_underlying_id": 2},
        })
        self.assertEqual(set(report["underlyings"]), {"456", "700", "701", "999"})
        self.assertEqual({o["ins_code"] for o in report["options"]}, {"125", "127", "128", "131", "132"})
        self.assertEqual(report["summary"]["research_counts"], {"REJECTED": 1, "UNKNOWN": 4, "RESEARCH_ONLY": 0})
        self.assertEqual(report["capture"]["screening_status"], "not_evaluated_invalid_snapshot_identity")
        self.assertIsNone(report["summary"]["screening"]["candidate_passes"])
        self.assertTrue(all(o["screening"] is None for o in report["options"]))
        self.assertFalse(report["execution_eligible"])
        self.assertIsNone(report["risk"]["max_quantity"])
        findings = {(d["raw_row"], d["side"]): (d["reason_code"], d["excluded_from_options"])
                    for d in report["diagnostics"]}
        self.assertEqual(findings[1, "C"], ("duplicate_option_id", True))
        self.assertEqual(findings[3, "P"], ("conflicting_option_id", True))
        self.assertEqual(findings[4, "C"], ("missing_option_id", True))
        self.assertEqual(findings[7, "C"], ("invalid_numeric_fields", False))
        self.assertEqual(findings[8, "P"], ("missing_underlying_id", True))

    def test_market_non_object_row_and_evidence_free_sides_are_not_contracts(self):
        empty_side_pair = {"uaInsCode": "1234", "insCode_C": "",
                           "insCode_P": 0, "qTotCap_P": 0.0, "endDate": "20270127"}
        options, bases, findings, empty = audit.inventory_contracts([empty_side_pair, None])
        self.assertFalse(options)
        self.assertEqual(set(bases), {"1234"})
        self.assertEqual(empty, 2)
        self.assertEqual([(d["raw_row"], d["side"], d["reason_code"]) for d in findings],
                         [(2, "C", "invalid_raw_row"), (2, "P", "invalid_raw_row")])

    def legacy_fixture(self):
        root = self.stream.parent / "legacy"
        root.mkdir()
        manifest = {"schema_version": 1, "dataset_class": watch.DATA_CLASS, "source_url": watch.SOURCE,
                    "exchange_quote_time_verified": False, "execution_eligible": False, "records": []}
        with collector.locked(self.stream, exclusive=False):
            for meta in collector.read_stream(self.stream):
                seq = meta["sequence"]
                body_name, receipt_name = f"snapshot-{seq}.raw.json", f"snapshot-{seq}.receipt.json"
                (root / body_name).write_bytes(meta["path"].read_bytes())
                legacy_meta = {key: value for key, value in meta.items() if key not in ("path", "head_sha256", "sequence")}
                receipt = collector.json_bytes(legacy_meta | {"attempt": seq})
                (root / receipt_name).write_bytes(receipt)
                manifest["records"].append({"body_file": body_name, "body_sha256": meta["body_sha256"],
                                            "receipt_file": receipt_name, "receipt_sha256": watch.digest(receipt)})
        (root / "manifest.json").write_bytes(collector.json_bytes(manifest))
        return root

    def test_market_legacy_capture_and_manual_capture_are_explicit_and_never_substituted(self):
        meta = self.append(21)
        self.append(22, uaInsCode=combine.RESEARCH_UNDERLYING)
        legacy = self.legacy_fixture()
        history = self.stream.parent / "absent.json"
        args = ("--legacy-archive", str(legacy), "--format", "json")
        with patch.object(watch, "contract_states", wraps=watch.contract_states) as replay:
            status, stdout, errors = self.market_cli(history, capture="legacy:1", options=args)
            self.assertEqual((status, errors), (0, ""))
            replay.assert_called_once()
            self.assertEqual(replay.call_args.args[0].name, "snapshot-1.raw.json")
        report = json.loads(stdout)
        self.assertEqual(set(report["underlyings"]), {"456"})
        self.assertEqual(report["capture"]["body_sha256"], meta["body_sha256"])
        self.assertIsNone(report["capture"]["seal_sha256"])
        self.assertEqual(report["capture"]["manifest_sha256"], watch.digest((legacy / "manifest.json").read_bytes()))
        self.assertEqual(report["capture"]["integrity_method"], "legacy_manifest_and_receipt")
        for name in ("manual:3", "legacy:3", "latest"):
            with self.subTest(name=name):
                status, stdout, errors = self.market_cli(history, capture=name, options=args)
                self.assertEqual((status, stdout), (1, ""))
                self.assertIn(name if name != "latest" else "manual:N یا legacy:N", errors)
        (legacy / "snapshot-1.raw.json").write_bytes(b"{}")
        status, stdout, errors = self.market_cli(history, capture="legacy:1", options=args)
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("SHA-256 mismatch", errors)

    def test_market_rejects_missing_or_tampered_manual_evidence_and_conflicting_flags(self):
        history = self.stream.parent / "absent.json"
        self.append(21)
        self.append(22, uaInsCode=combine.RESEARCH_UNDERLYING)
        path = self.stream / "00000002" / "receipt.json"
        path.chmod(0o600)
        path.write_bytes(b"{")
        status, stdout, errors = self.market_cli(history, options=("--format", "json"))
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("SHA-256 mismatch", errors)
        path.unlink()
        status, stdout, errors = self.market_cli(history)
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("missing or unsafe record files", errors)
        with self.assertRaises(SystemExit) as failure:
            self.market_cli(history, options=("--underlying-code", combine.RESEARCH_UNDERLYING))
        self.assertEqual(failure.exception.code, 2)

    def test_market_rejects_hash_valid_legacy_receipt_without_required_schema_fields(self):
        self.append(21)
        legacy = self.legacy_fixture()
        receipt_path, manifest_path = legacy / "snapshot-1.receipt.json", legacy / "manifest.json"
        receipt, manifest = json.loads(receipt_path.read_bytes()), json.loads(manifest_path.read_bytes())
        del receipt["rows"]
        raw = collector.json_bytes(receipt)
        receipt_path.write_bytes(raw)
        manifest["records"][0]["receipt_sha256"] = watch.digest(raw)
        manifest_path.write_bytes(collector.json_bytes(manifest))
        status, stdout, errors = self.market_cli(self.stream.parent / "absent.json", capture="legacy:1",
                                                 options=("--legacy-archive", str(legacy)))
        self.assertEqual((status, stdout), (1, ""))
        self.assertIn("body does not match receipt", errors)

    def test_legacy_report_still_uses_existing_shape_sort_and_output_files(self):
        ranked = [{"symbol": "غیرهم‌جهت", "underlying_symbol": "نمونه", "option_type": "PUT", "rank": 1,
                   "underlying_price_date": "20261121"},
                  {"symbol": "هم‌جهت", "underlying_symbol": "نمونه", "option_type": "CALL", "rank": 2,
                   "underlying_price_date": 20261121}]
        trends = {"metadata": {"generated_at": datetime.now(timezone.utc).isoformat(), "model_version": "2.1-experimental"},
                  "results": [{"symbol": "نمونه", "trend": "صعودی", "analysis_status": "ok",
                               "latest_date": 20261121, "history_age_hours": 1}]}
        with patch.object(combine, "load_json", side_effect=[ranked, trends]) as read, \
                patch.object(combine, "atomic_write") as write, redirect_stdout(StringIO()):
            combine.main()
        self.assertEqual([call.args[0] for call in read.call_args_list], [combine.RANKED_FILE, combine.TRENDS_FILE])
        self.assertEqual([call.args[0] for call in write.call_args_list], [combine.OUTPUT_JSON, combine.OUTPUT_TXT])
        results = json.loads(write.call_args_list[0].args[1])
        self.assertEqual([item["directional_eligible"] for item in results], [True, False])
        self.assertIn("این گزارش سیگنال خرید یا فروش نیست.", write.call_args_list[1].args[1])
