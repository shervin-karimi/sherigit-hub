"""Deterministic test-only fixtures; no access to the real archive or network."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import shadow_watch as watch


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ShadowWatchTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.start = datetime(2026, 11, 21, 7, tzinfo=timezone.utc)
        self.tehran = timezone(timedelta(hours=3, minutes=30))
        self.manifest = {
            "schema_version": 1,
            "dataset_class": watch.DATA_CLASS,
            "source_url": watch.SOURCE,
            "exchange_quote_time_verified": False,
            "execution_eligible": False,
            "records": [],
        }

    def row(self, **updates):
        return {
            "uaInsCode": "101",
            "insCode_C": "202",
            "insCode_P": "303",
            "lVal18AFC_C": "آزمون خرید",
            "lVal18AFC_P": "آزمون فروش",
            "strikePrice": 1000,
            "contractSize": 1000,
            "endDate": "20270120",
            "remainedDay": 60,
            "pClosing_UA": 1000,
            "pDrCotVal_UA": 1000,
            "pMeDem_C": 100,
            "pMeOf_C": 102,
            "qTitMeDem_C": 10,
            "qTitMeOf_C": 9,
            "zTotTran_C": 50,
            "qTotTran5J_C": 200,
            **updates,
        }

    def save_manifest(self):
        (self.archive / "manifest.json").write_text(json.dumps(self.manifest))

    def record(self, attempt, rows, *, minutes=None, dataset_class=None):
        body_name, meta_name = f"snapshot-{attempt}.raw.json", f"snapshot-{attempt}.receipt.json"
        raw = json.dumps({"instrumentOptMarketWatch": rows}).encode()
        at = self.start + timedelta(minutes=attempt if minutes is None else minutes)
        meta = json.dumps({
            "source_url": watch.SOURCE,
            "dataset_class": dataset_class or watch.DATA_CLASS,
            "exchange_quote_timestamp": None,
            "provider_quote_timestamp": None,
            "order_book_sequence": None,
            "execution_eligible": False,
            "attempt": attempt,
            "http_status": 200,
            "receipt_utc": at.isoformat(),
            "request_utc": (at - timedelta(seconds=1)).isoformat(),
            "receipt_tehran": at.astimezone(self.tehran).isoformat(),
            "byte_count": len(raw),
            "body_sha256": sha(raw),
            "rows": len(rows),
        }).encode()
        (self.archive / body_name).write_bytes(raw)
        (self.archive / meta_name).write_bytes(meta)
        self.manifest["records"].append({
            "body_file": body_name, "receipt_file": meta_name,
            "body_sha256": sha(raw), "receipt_sha256": sha(meta),
        })
        self.save_manifest()

    def test_prefix_is_unchanged_with_future_snapshot_and_manifest_reordering(self):
        self.record(1, [self.row()])
        before = watch.replay(self.archive)["observations"]
        self.record(2, [self.row(pMeDem_C=103, pMeOf_C=106, qTitMeDem_C=12)])
        ordered = watch.replay(self.archive)
        self.manifest["records"].reverse()
        self.save_manifest()
        self.assertEqual(watch.replay(self.archive), ordered)
        self.assertEqual(ordered["observations"][:1], before)
        first, second = ordered["observations"]
        self.assertLess(first["observed_at_utc"], second["observed_at_utc"])
        self.assertIsNone(first["exchange_quote_at"])
        changed = second["since_previous"]
        self.assertEqual(changed["changed"]["202"]["best_bid"], [100, 103])
        self.assertEqual(changed["price_changes"], 1)
        self.assertEqual(changed["spread_changes"], 1)
        self.assertEqual(changed["liquidity_changes"], 1)
        self.assertFalse(ordered["price_changes_are_executable_returns"])

    def test_eligibility_missing_quotes_and_unavailable_instruments(self):
        self.record(1, [self.row()])
        self.record(2, [self.row(insCode_P=None, pMeDem_C=None),
                        self.row(insCode_C="404", insCode_P="")])
        first, second = watch.replay(self.archive)["observations"]
        self.assertEqual(first["candidate_count"], 1)
        self.assertEqual(first["rank_eligible_count"], 0)
        self.assertEqual(first["contracts"]["202"]["greeks_assumption_status"],
                         "experimental_unverified")
        self.assertIn("تأیید نشده", first["contracts"]["202"]["rank_rejection"])
        self.assertIsNone(second["contracts"]["202"]["best_bid"])
        self.assertIsNone(second["contracts"]["202"]["spread_percent"])
        self.assertEqual(second["contracts"]["202"]["candidate_rejection"],
                         "invalid_quote_or_closing_spot")
        diff = second["since_previous"]
        self.assertEqual(diff["appeared"], ["404"])
        self.assertEqual(diff["disappeared"], ["303"])
        self.assertEqual(diff["eligibility_or_reason_changes"], 1)

    def test_body_hash_mismatch_fails_closed_without_partial_cli_output(self):
        self.record(1, [self.row()])
        self.record(2, [self.row()])
        (self.archive / "snapshot-2.raw.json").write_bytes(b"{}")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(watch.main(["--archive", str(self.archive), "--format", "json"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("SHA-256 mismatch", stderr.getvalue())

    def test_receipt_corruption_and_missing_capture_are_rejected(self):
        self.record(1, [self.row()])
        self.record(3, [self.row()])
        with self.assertRaisesRegex(watch.ArchiveError, "missing observation"):
            watch.replay(self.archive)
        self.record(2, [self.row()])
        meta = self.archive / "snapshot-2.receipt.json"
        original = meta.read_bytes()
        meta.write_bytes(original + b"\n")
        with self.assertRaisesRegex(watch.ArchiveError, "SHA-256 mismatch"):
            watch.replay(self.archive)
        meta.write_bytes(original)
        (self.archive / "snapshot-2.raw.json").unlink()
        with self.assertRaisesRegex(watch.ArchiveError, "missing or unsafe file"):
            watch.replay(self.archive)

    def test_duplicate_observation_and_tied_receipts_are_rejected(self):
        self.record(1, [self.row()])
        self.manifest["records"].append(self.manifest["records"][0])
        self.save_manifest()
        with self.assertRaisesRegex(watch.ArchiveError, "duplicate observation"):
            watch.replay(self.archive)
        self.manifest["records"].pop()
        self.record(2, [self.row()], minutes=1)
        with self.assertRaisesRegex(watch.ArchiveError, "duplicate receipt timestamp"):
            watch.replay(self.archive)

    def test_reversed_capture_clock_is_rejected(self):
        self.record(1, [self.row()], minutes=2)
        self.record(2, [self.row()], minutes=1)
        with self.assertRaisesRegex(watch.ArchiveError, "out-of-order capture clock"):
            watch.replay(self.archive)

    def test_duplicate_ids_and_reused_identity_fail_closed(self):
        self.record(1, [self.row(insCode_P="202")])
        with self.assertRaisesRegex(watch.ArchiveError, "duplicate instrument identifier"):
            watch.replay(self.archive)
        self.manifest["records"].clear()
        self.record(1, [self.row()])
        self.record(2, [self.row(strikePrice=1100)])
        with self.assertRaisesRegex(watch.ArchiveError, "contract identity modified"):
            watch.replay(self.archive)

    def test_synthetic_and_unsafe_manifest_paths_are_rejected(self):
        self.record(1, [self.row()], dataset_class="synthetic")
        with self.assertRaisesRegex(watch.ArchiveError, "sourced receipt"):
            watch.replay(self.archive)
        self.manifest["records"][0]["body_file"] = "../snapshot-1.raw.json"
        self.save_manifest()
        with self.assertRaisesRegex(watch.ArchiveError, "unsafe"):
            watch.replay(self.archive)

    def test_same_body_with_distinct_receipt_is_a_separate_observation(self):
        self.record(1, [self.row()])
        self.record(2, [self.row()])
        first, second = watch.replay(self.archive)["observations"]
        self.assertEqual(first["body_sha256"], second["body_sha256"])
        self.assertEqual(second["since_previous"]["changed"], {})

    def test_reproducible_cli_and_no_archive_network_or_trade_side_effects(self):
        self.record(1, [self.row()])
        ledger = self.root / "trading.db"
        ledger.write_bytes(b"trading file sentinel")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch("sqlite3.connect", side_effect=AssertionError("trading DB forbidden")), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("shadow_watch.subprocess.run", wraps=subprocess.run) as child:
            snapshots = []
            for _ in range(2):
                with redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(watch.main(["--archive", str(self.archive), "--format", "json"]), 0)
                snapshots.append(stdout.getvalue())
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(watch.main(["--archive", str(self.archive)]), 0)
            self.assertIn("بدون سفارش یا بازده", stdout.getvalue())
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertFalse(json.loads(snapshots[0])["execution_eligible"])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        self.assertEqual({Path(call.args[0][-1]).name for call in child.call_args_list},
                         set(watch.ANALYSIS_SCRIPTS))
        for call in child.call_args_list:
            self.assertNotIn(self.root, call.kwargs["cwd"].parents)


if __name__ == "__main__":
    unittest.main()
