"""Offline collector and schedule tests; sockets and real archives are never used."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from io import StringIO
import json
from pathlib import Path
import socket
import ssl
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError

import shadow_collector as collector
import shadow_schedule as schedule
import shadow_watch as watch


START = datetime(2026, 11, 21, 6, tzinfo=timezone.utc)  # 09:30 Tehran
APPEND, READ_STREAM, FETCH = collector.append, collector.read_stream, collector.fetch


def body(price=100):
    return collector.json_bytes({"instrumentOptMarketWatch": [{
        "uaInsCode": "101", "insCode_C": "202", "insCode_P": "303",
        "lVal18AFC_C": "test-call", "lVal18AFC_P": "test-put",
        "strikePrice": 1000, "contractSize": 1000, "endDate": "20270120",
        "pClosing_UA": 1000, "pDrCotVal_UA": 1000, "pMeDem_C": price,
        "pMeOf_C": price + 2, "qTitMeDem_C": 10, "qTitMeOf_C": 9,
    }]})


class ShadowCollectionTests(unittest.TestCase):
    def mock(self, target, **kwargs):
        mock = patch(target, **kwargs)
        self.addCleanup(mock.stop)
        return mock.start()

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stream = self.root / "manual"
        self.legacy = self.root / "legacy"
        self.mock("shadow_collector.STREAM", new=self.stream)
        self.mock("shadow_watch.DEFAULT_ARCHIVE", new=self.legacy)
        self.mock("socket.socket", side_effect=AssertionError("network forbidden"))
        # Termux's Python may have no IANA database; use the known offset for fixture dates.
        self.mock("shadow_schedule.ZoneInfo", return_value=collector.TEHRAN)
        self.fetch = self.mock("shadow_collector.fetch")
        self.clock = self.mock("shadow_collector.utc_now", return_value=START)
        self.sleep = self.mock("shadow_collector.time.sleep")
        self.mock("shadow_collector.append", side_effect=lambda *a, **kw:
                  APPEND(*a, **kw, dataset_class=collector.SYNTHETIC))
        self.mock("shadow_collector.read_stream", side_effect=lambda root:
                  READ_STREAM(root, collector.SYNTHETIC))
        self.stdout, self.stderr = StringIO(), StringIO()
        out, err = redirect_stdout(self.stdout), redirect_stderr(self.stderr)
        out.__enter__()
        err.__enter__()
        self.addCleanup(out.__exit__, None, None, None)
        self.addCleanup(err.__exit__, None, None, None)

    def events(self):
        return [json.loads(line) for line in self.stderr.getvalue().splitlines()]

    def records(self):
        with collector.locked(self.stream, exclusive=False):
            return READ_STREAM(self.stream, collector.SYNTHETIC)

    def calendar(self, hours="09:00-12:30"):
        path = self.root / "sessions.json"
        path.write_bytes(collector.json_bytes({"source": "offline fixture",
                                               "sessions": {"2026-11-21": [hours]}}))
        return path

    def schedule(self, path, window="09:30-09:35", *extra):
        return schedule.main(["--calendar", str(path), "--window", window, *extra])

    def test_dns_and_tls_retries_then_write_only_the_successful_attempt(self):
        times = [START + timedelta(seconds=i) for i in range(6)]
        self.clock.side_effect = times[:5]
        self.fetch.side_effect = [
            URLError(socket.gaierror(socket.EAI_AGAIN, "temporary")),
            ssl.SSLEOFError(8, "unexpected EOF"),
            (body(), times[5], {"Date": "not an exchange timestamp"}),
        ]
        self.assertEqual(collector.main(["--timeout", "6", "--backoff", "2.5"]), 0)
        self.assertEqual(self.fetch.call_args_list, [call(timeout=6)] * 3)
        self.assertEqual(self.sleep.call_args_list, [call(2.5), call(5)])
        row, = self.records()
        self.assertEqual(row["request_utc"], times[4].isoformat())
        self.assertEqual(row["receipt_utc"], times[5].isoformat())
        self.assertIsNone(row["exchange_quote_timestamp"])
        self.assertEqual(row["path"].read_bytes(), body())
        self.assertEqual(json.loads(self.stdout.getvalue())["head_sha256"], row["head_sha256"])
        self.assertEqual([item.get("reason") for item in self.events()], ["dns", "tls", None])
        self.assertEqual(self.events()[-1]["event"], "capture_succeeded")

    def test_tcp_exhaustion_is_bounded_and_restart_can_capture(self):
        self.fetch.side_effect = URLError(ConnectionResetError("reset"))
        self.assertEqual(collector.main([]), 1)
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertEqual(list(self.stream.iterdir()), [])
        self.assertFalse(self.stdout.getvalue())
        self.assertEqual([item["event"] for item in self.events()],
                         ["capture_retry", "capture_retry", "capture_failed"])
        self.fetch.side_effect = None
        self.fetch.return_value = (body(), START, {})
        self.assertEqual(collector.collect()["sequence"], 1)

    def test_permanent_transport_and_http_errors_are_not_retried(self):
        http_error = HTTPError(watch.SOURCE, 503, "unavailable", {}, None)
        self.addCleanup(http_error.close)
        for error in [URLError(socket.gaierror(socket.EAI_NONAME, "no such host")),
                      URLError(ssl.SSLCertVerificationError(1, "bad cert")),
                      ssl.SSLError(1, "bad protocol"),
                      http_error]:
            with self.subTest(error=error):
                self.fetch.reset_mock()
                self.fetch.side_effect = error
                self.assertEqual(collector.main([]), 1)
                self.fetch.assert_called_once()
                self.assertEqual(list(self.stream.iterdir()), [])
        self.sleep.assert_not_called()

    def test_truncated_http_body_is_retryable_even_if_prefix_is_valid_json(self):
        raw = body()
        response = MagicMock(status=200, headers={"Content-Length": str(len(raw) + 1)})
        response.geturl.return_value = watch.SOURCE
        response.read.return_value = raw
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(collector, "build_opener", return_value=opener):
            with self.assertRaises(IncompleteRead) as caught:
                FETCH(timeout=7)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
        self.assertEqual(collector.transient_transport(caught.exception), "transport")

    def test_http_body_requires_unambiguous_framing(self):
        response = MagicMock(status=200)
        response.geturl.return_value = watch.SOURCE
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(collector, "build_opener", return_value=opener):
            for headers in ({}, {"Content-Length": "1", "Transfer-Encoding": "chunked"}):
                response.headers = headers
                with self.assertRaisesRegex(watch.ArchiveError, "framing"):
                    FETCH()
            response.read.assert_not_called()
            response.headers = {"Transfer-Encoding": "chunked"}
            response.read.return_value = body()
            self.assertEqual(FETCH()[0], body())

    def test_invalid_options_and_payload_do_not_retry(self):
        self.assertEqual(collector.main(["--timeout", "nan"]), 1)
        self.assertEqual(collector.main(["--backoff", "inf"]), 1)
        self.fetch.assert_not_called()
        self.fetch.return_value = (b"{}", START, {})
        self.assertEqual(collector.main([]), 1)
        self.fetch.assert_called_once()
        self.assertFalse(list(self.stream.iterdir()))
        self.sleep.assert_not_called()

    def test_restart_keeps_committed_record_and_preserves_stale_staging(self):
        self.fetch.return_value = (body(), START, {})
        first = collector.collect()
        pending = self.root / ".manual.pending-crash"
        pending.mkdir()
        partial = pending / "body.raw.json"
        partial.write_bytes(b"{")
        self.fetch.reset_mock()

        self.clock.return_value = START + collector.MIN_INTERVAL - timedelta(seconds=1)
        with self.assertRaisesRegex(watch.ArchiveError, "minimum 60 seconds"):
            collector.collect()
        self.fetch.assert_not_called()
        self.clock.return_value = START + collector.MIN_INTERVAL
        self.fetch.return_value = (body(), self.clock.return_value, {})
        with self.assertRaisesRegex(watch.ArchiveError, "duplicate body"):
            collector.collect()
        self.fetch.assert_called_once()
        self.fetch.return_value = (body(101), self.clock.return_value, {})
        self.assertEqual(collector.collect()["sequence"], 2)
        first_row, second_row = self.records()
        self.assertEqual(first_row["head_sha256"], first["head_sha256"])
        seal, _ = watch.read_object(second_row["path"].with_name("seal.json"), watch.MAX_META)
        self.assertEqual(seal["previous_seal_sha256"], first["head_sha256"])
        self.assertEqual(partial.read_bytes(), b"{")

    def test_failed_publish_leaves_prior_chain_intact_and_allows_restart(self):
        self.fetch.return_value = (body(), START, {})
        with patch.object(Path, "rename", side_effect=OSError("interrupted publish")):
            self.assertEqual(collector.main([]), 1)
        self.assertEqual(self.records(), [])
        self.assertEqual(collector.collect()["sequence"], 1)

    def test_legacy_duplicate_and_tamper_never_append(self):
        self.legacy.mkdir()
        raw = self.legacy / "snapshot.raw.json"
        raw.write_bytes(body())
        legacy_rows = [(START - collector.MIN_INTERVAL, 1, raw,
                        {"body_sha256": watch.digest(body())})]
        with patch.object(watch, "records_in_order", return_value=legacy_rows):
            self.fetch.return_value = (body(), START, {})
            with self.assertRaisesRegex(watch.ArchiveError, "duplicate body"):
                collector.collect()
            self.fetch.reset_mock()
            raw.write_bytes(b"{}")
            with self.assertRaisesRegex(watch.ArchiveError, "SHA-256 mismatch"):
                collector.collect()
        self.fetch.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_clock_rolls_back_between_attempts_or_during_receipt(self):
        earlier = START - timedelta(seconds=1)
        self.clock.side_effect = [START, START, earlier]
        self.fetch.side_effect = TimeoutError("timeout")
        with self.assertRaisesRegex(watch.ArchiveError, "clock moved backward"):
            collector.collect()
        self.fetch.assert_called_once()
        self.clock.side_effect = None
        self.fetch.side_effect = None
        self.fetch.return_value = (body(), earlier, {})
        with self.assertRaisesRegex(watch.ArchiveError, "clock moved backward"):
            collector.collect()
        self.assertEqual(self.records(), [])

    def test_no_network_when_calendar_missing_invalid_or_missing_timezone(self):
        path = self.root / "missing.json"
        self.assertEqual(self.schedule(path), 1)
        path.write_text('{"sessions":{"2026-11-21":["09:00-12:30"]}}')
        self.assertEqual(self.schedule(path), 1)
        self.assertEqual(self.schedule(self.calendar("09:00-08:00")), 1)
        with patch.object(schedule, "ZoneInfo", side_effect=schedule.ZoneInfoNotFoundError):
            self.assertEqual(self.schedule(self.calendar()), 1)
        self.fetch.assert_not_called()
        self.assertFalse(self.stream.exists())

    def test_skip_unlisted_day_early_close_closed_window_and_dry_run(self):
        self.clock.return_value = START + timedelta(days=1)
        self.assertEqual(self.schedule(self.calendar()), 0)
        self.clock.return_value = START
        self.assertEqual(self.schedule(self.calendar("09:00-09:30")), 0)
        self.assertEqual(self.schedule(self.calendar(), "09:00-09:30"), 0)
        self.assertEqual(self.schedule(self.calendar(), "09:30-09:35", "--dry-run"), 0)
        self.assertEqual([event["reason"] for event in self.events()],
                         ["no_listed_session", "outside_session_hours",
                          "outside_collection_window", "dry_run"])
        self.fetch.assert_not_called()
        self.assertFalse(self.stream.exists())

    def test_gate_rechecks_before_retry_and_declines_late_receipt(self):
        close = START + timedelta(minutes=5)
        self.clock.side_effect = [START, START, START, close]
        self.fetch.side_effect = ConnectionResetError("reset")
        self.assertEqual(self.schedule(self.calendar()), 0)
        self.fetch.assert_called_once()
        self.assertEqual(self.events()[-1]["reason"], "outside_collection_window")
        self.assertEqual(self.records(), [])
        self.clock.side_effect = [START, START]
        self.fetch.side_effect = None
        self.fetch.return_value = (body(), close, {})
        self.assertEqual(self.schedule(self.calendar()), 0)
        self.assertEqual(self.records(), [])

    def test_scheduled_capture_in_session(self):
        self.fetch.return_value = (body(), START, {})
        self.assertEqual(self.schedule(self.calendar(), "09:30-09:35", "--timeout", "7"), 0)
        self.fetch.assert_called_once_with(timeout=7)
        self.assertEqual(self.events()[-1]["event"], "capture_succeeded")
        self.assertEqual(self.records()[0]["receipt_tehran"], "2026-11-21T09:30:00+03:30")


if __name__ == "__main__":
    unittest.main()
