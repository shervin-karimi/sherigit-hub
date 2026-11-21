"""Synthetic fixtures only: no HTTP, historical archive, or account access."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import shadow_collector as collector
import shadow_multiday as tracking
import shadow_watch as watch


ORIGINAL_FETCH = collector.fetch


class SyntheticMultidayTests(unittest.TestCase):
    label = collector.SYNTHETIC
    sessions = [f"2026-11-{day}" for day in (21, 22, 23, 24, 25, 28, 29, 30)]

    def setUp(self):
        scratch = TemporaryDirectory(prefix="synthetic-shadow-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.stream = self.root / self.label
        self.stream.mkdir()
        for target in ("socket.socket", "sqlite3.connect", "shadow_collector.fetch"):
            guard = patch(target, side_effect=AssertionError(f"{target} forbidden in offline tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def body(self, i, **overrides):
        row = {
            "uaInsCode": "101", "insCode_C": "202", "insCode_P": "303",
            "lVal18AFC_C": "synthetic-call", "lVal18AFC_P": "synthetic-put",
            "strikePrice": 1000, "contractSize": 1000, "endDate": "20270120",
            "remainedDay": 55, "pClosing_UA": 1000, "pDrCotVal_UA": 1000,
            "pMeDem_C": 100 + i, "pMeOf_C": 102 + i, "qTitMeDem_C": 10,
            "qTitMeOf_C": 9, "zTotTran_C": 50, "qTotTran5J_C": 200,
            **overrides,
        }
        return collector.json_bytes({"instrumentOptMarketWatch": [row]})

    def save(self, i, *, raw=None, hour=7, **overrides):
        at = datetime.fromisoformat(f"{self.sessions[i]}T{hour:02d}:00:00+00:00")
        with collector.locked(self.stream, exclusive=True) as fd:
            prior = collector.read_stream(self.stream, self.label)
            return collector.append(self.stream, fd, prior, self.body(i, **overrides) if raw is None else raw,
                                    at - timedelta(seconds=1), at, {}, dataset_class=self.label)

    def calendar(self):
        path = self.root / "synthetic-sessions.json"
        path.write_bytes(collector.json_bytes({"source": self.label, "sessions": self.sessions}))
        return tracking.session_calendar(path)

    def report(self, **kwargs):
        return tracking.build_report(self.stream, calendar=self.calendar(), dataset_class=self.label, **kwargs)

    def test_1_3_7_require_distinct_calendar_sessions_and_use_latest_daily_receipt(self):
        self.save(0)
        self.save(0, hour=8, pMeDem_C=101, pMeOf_C=103)
        early = self.report()
        self.assertEqual(early["horizons"]["1"]["status"], "insufficient_calendar_lookback")
        self.assertNotIn("contracts", early["horizons"]["1"])
        self.assertEqual(early["days"][self.sessions[0]]["capture_count"], 2)
        for i in range(1, 8):
            self.save(i, lVal18AFC_C="synthetic-renamed")
        report = self.report()
        self.assertEqual(report["dataset_class"], self.label)
        self.assertFalse(report["execution_eligible"])
        self.assertIsNone(report["days"][self.sessions[-1]]["exchange_quote_at"])
        for h in (1, 3, 7):
            result = report["horizons"][str(h)]
            self.assertEqual(len(result["sessions"]), h + 1)
            self.assertEqual(result["contracts"]["202"]["midpoint_change"], h if h < 7 else 6)
            self.assertEqual(result["contracts"]["303"]["status"], "unusable_endpoint_quotes")
        state = report["days"][self.sessions[-1]]["contracts"]["202"]
        self.assertEqual(state["greeks_assumption_status"], "experimental_unverified")
        self.assertIsNotNone(state["rank_rejection"])
        self.assertEqual(state["bid_volume"], 10)

    def test_missing_session_and_missing_instrument_do_not_make_a_price_result(self):
        self.save(0)
        self.save(1, insCode_C=None)
        self.save(2)
        self.save(3)
        self.save(5)  # session index 4 is missing, not silently treated as a holiday
        gap = self.report()["horizons"]["1"]
        self.assertEqual(gap["missing_sessions"], [self.sessions[4]])
        self.assertNotIn("contracts", gap)
        self.assertEqual(tracking.day_horizon(self.report()["days"], self.sessions[:4])
                         ["contracts"]["202"], {"status": "absent_in_intervening_session"})
        no_calendar = tracking.build_report(self.stream, dataset_class=self.label)
        self.assertEqual(no_calendar["horizons"]["1"]["status"], "session_calendar_required")

    def test_contract_reuse_with_changed_terms_fails_closed(self):
        self.save(0)
        self.save(1, strikePrice=1100)
        with self.assertRaisesRegex(watch.ArchiveError, "contract identity modified"):
            self.report()

    def test_reject_duplicate_bytes_and_backwards_clock_without_mutation(self):
        first = self.save(1)
        with self.assertRaisesRegex(watch.ArchiveError, "duplicate body"):
            self.save(2, raw=self.body(1))
        with self.assertRaisesRegex(watch.ArchiveError, "out-of-order"):
            self.save(0)
        with self.assertRaisesRegex(watch.ArchiveError, "invalid TSETMC"):
            self.save(2, raw=b"{}")
        self.assertEqual(self.report(expected_head=first["head_sha256"])["manual_head_sha256"],
                         first["head_sha256"])
        self.assertEqual([path.name for path in self.stream.iterdir()], ["00000001"])
        self.assertEqual((self.stream / "00000001/body.raw.json").read_bytes(), self.body(1))

    def test_detect_partial_and_corrupt_entries(self):
        self.save(0)
        self.save(1)
        self.save(2)
        second = self.stream / "00000002"
        second.rename(self.root / "removed")
        with self.assertRaisesRegex(watch.ArchiveError, "missing, unexpected or unfinished"):
            self.report()
        (self.root / "removed").rename(second)
        body = second / "body.raw.json"
        body.chmod(0o600)
        body.write_bytes(b"{}")
        with self.assertRaisesRegex(watch.ArchiveError, "SHA-256 mismatch"):
            self.report()

    def test_pinned_head_detects_tail_loss_and_replay_does_not_mix_classes(self):
        self.save(0)
        tail = self.save(1)
        with self.assertRaisesRegex(watch.ArchiveError, "unsupported receipt"):
            tracking.build_report(self.stream)
        (self.stream / "00000002").rename(self.root / "tail")
        with self.assertRaisesRegex(watch.ArchiveError, "pinned SHA-256"):
            self.report(expected_head=tail["head_sha256"])

    def test_calendar_rejects_duplicates(self):
        path = self.root / "bad-sessions.json"
        path.write_bytes(collector.json_bytes({"source": self.label,
                                                "sessions": [self.sessions[0]] * 2}))
        with self.assertRaisesRegex(watch.ArchiveError, "unique"):
            tracking.session_calendar(path)

    def test_http_adapter_uses_fixed_public_get_and_preserves_body_bytes(self):
        response = MagicMock(status=200, headers={"Date": "HTTP Date is not quote time"})
        response.geturl.return_value = watch.SOURCE
        raw = b'{ "instrumentOptMarketWatch" : [] }\n'
        response.read.return_value = raw
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        at = datetime(2026, 12, 1, 7, tzinfo=timezone.utc)
        # Exercise the adapter with a fake opener; the socket guard stays active.
        with patch.object(collector, "build_opener", return_value=opener) as build, \
                patch.object(collector, "utc_now", return_value=at):
            got = ORIGINAL_FETCH()
        self.assertEqual(got, (raw, at, {"Date": "HTTP Date is not quote time"}))
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual((request.get_method(), request.full_url), ("GET", watch.SOURCE))
        response.read.assert_called_once_with(watch.MAX_BODY + 1)
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, "", {}, ""))

    def test_manual_collector_one_fake_get_then_spacing_rejects_before_get(self):
        target = self.root / "mock-http"
        start = datetime(2026, 12, 1, 7, tzinfo=timezone.utc)
        received = start + timedelta(seconds=1)
        with patch.object(watch, "DEFAULT_ARCHIVE", self.root / "absent"), \
                patch.object(collector, "STREAM", target), \
                patch.object(collector, "utc_now", side_effect=[start, received]), \
                patch.object(collector, "fetch", return_value=(b"{}", received, {})) as fake:
            with self.assertRaisesRegex(watch.ArchiveError, "invalid TSETMC"):
                collector.collect()
            fake.assert_called_once_with()
            self.assertEqual(list(target.iterdir()), [])
        # Storage and the full report use explicitly synthetic records above. Here mock
        # the commit to verify the production wrapper without stamping fixtures as real.
        first = dict(receipt_utc=received.isoformat(), body_sha256="not-real")
        with patch.object(watch, "DEFAULT_ARCHIVE", self.root / "absent"), \
                patch.object(collector, "STREAM", target), \
                patch.object(collector, "utc_now", side_effect=[start, received]), \
                patch.object(collector, "read_stream", side_effect=[[], [first]]), \
                patch.object(collector, "fetch", return_value=(b"{}", received, {})) as fake, \
                patch.object(collector, "append", return_value=first) as commit:
            self.assertEqual(collector.collect(), first)
            with self.assertRaisesRegex(watch.ArchiveError, "minimum 60 seconds"):
                collector.collect()
            fake.assert_called_once_with()
            commit.assert_called_once()
