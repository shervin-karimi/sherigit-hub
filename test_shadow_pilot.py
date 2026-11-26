"""Deterministic pilot tests with temporary ledgers and synthetic HTTP bodies."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from paper_limits import BotPolicy, PaperLimits
from paper_quotes import load_catalog
from paper_trading import PaperTrading
import shadow_collector as collector
import shadow_pilot as pilot
import shadow_watch as watch
from test_shadow_collection import APPEND, READ_STREAM, body


class PilotTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "pilot"
        self.now = datetime(2026, 11, 22, 6, tzinfo=timezone.utc)
        self.config = self.root / "pilot.json"
        self.calendar = self.root / "tehran.json"
        self.settings = {"root": str(self.output), "calendar": str(self.calendar),
                         "windows": ["09:00-12:30"], "capture_interval_seconds": 120}
        self.write(self.config, self.settings)
        days = ["2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18",
                "2026-11-21", "2026-11-22", "2026-11-29", "2026-12-06", "2026-12-22"]
        self.write(self.calendar, {"source": "synthetic calendar",
                                   "sessions": {day: ["09:00-12:30"] for day in days}})
        self.socket_guard = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.addCleanup(self.socket_guard.stop)
        self.socket_guard.start()
        self.use_patch("shadow_collector.utc_now", side_effect=lambda: self.now)
        self.use_patch("shadow_watch.DEFAULT_ARCHIVE", new=self.root / "no-legacy")
        self.use_patch("shadow_collector.append", side_effect=lambda *a, **kw:
                       APPEND(*a, **kw, dataset_class=collector.SYNTHETIC))
        self.use_patch("shadow_collector.read_stream", side_effect=lambda root:
                       READ_STREAM(root, collector.SYNTHETIC))
        self.fetch = self.use_patch("shadow_collector.fetch",
                                    side_effect=lambda **_: (body(), self.now, {"Date": "not exchange time"}))

    def use_patch(self, target, **kwargs):
        mock = patch(target, **kwargs)
        self.addCleanup(mock.stop)
        return mock.start()

    @staticmethod
    def write(path, value):
        path.write_bytes(collector.json_bytes(value))
        path.chmod(0o600)

    def enable_paper(self):
        self.policy = BotPolicy("pilot", "one", PaperLimits(
            2, 5000, 200, 6000, 7000, 1, 5000, 25, 3000,
        ))
        self.contracts = [{
            "ins_code": "202", "option_type": "CALL", "symbol": "sample CALL", "contract_size": 10,
            "source": "independent-fixture", "first_trading_date": "2026-11-15",
            "last_trading_date": "2026-12-21", "expires_at": "2026-12-23T12:00:00+00:00",
        }]
        self.paths = {key: self.root / key for key in pilot.PAPER_FIELDS}
        self.write(self.paths["policy"], asdict(self.policy))
        self.write(self.paths["catalog"], self.contracts)
        self.key = b"pilot-test-key-not-for-production"
        self.paths["key"].write_bytes(self.key)
        self.paths["key"].chmod(0o600)
        self.ledger = PaperTrading(self.paths["db"], clock=lambda: self.now,
                                   catalog=load_catalog(self.contracts), policy=self.policy)
        self.ledger.initialize(100_000)
        self.paths["db"].chmod(0o600)
        self.settings["paper"] = {key: str(path) for key, path in self.paths.items()}
        self.write(self.config, self.settings)

    def sign(self, sequence, *, entries=(), quote_changes=None):
        quote = {
            "ins_code": "202", "symbol": "sample CALL", "contract_size": 10,
            "option_type": "CALL", "source": "independent-fixture", "quote_class": "provider",
            "best_bid": 99, "best_ask": 100, "quoted_at": self.now.isoformat(),
            "received_at": self.now.isoformat(),
        } | (quote_changes or {})
        updates = [{"ins_code": "202", "order_id": order, "quantity": quantity, "market": {
            "observed_at": quote["quoted_at"], "strike": 1_000, "underlying_closing_price": 1_000,
            "trade_count": 40, "trade_volume": 200, "bid_volume": 10, "ask_volume": 10,
        }} for order, quantity in entries]
        payload = {"schema": 1, "sequence": sequence, "issued_at": self.now.isoformat(),
                   "evidence_ref": "TEST ONLY", "quotes": [quote], "entries": updates}
        hs = hmac.digest(self.key, collector.json_bytes(payload), hashlib.sha256).hex()
        self.write(self.paths["feed"], {"payload": payload, "hmac_sha256": hs})

    def report(self):
        return pilot.tick(self.config)

    def test_scheduled_capture_seals_and_scans_all_sides_after_restart(self):
        self.assertTrue(pilot.tick(self.config, dry_run=True)["پنجرهٔ فعال"])
        self.fetch.assert_not_called()
        first = self.report()
        observed = first["مشاهدات و غربال پژوهشی"]
        self.assertEqual(observed["پوشش"]["CALL"]["قرارداد"], 1)
        self.assertEqual(observed["پوشش"]["PUT"]["قرارداد"], 1)
        self.assertIsNone(observed["آخرین ثبت"]["exchange_quote_timestamp"])
        self.assertFalse(first["صلاحیت اجرای مشاهده"])
        self.assertEqual(first["دفتر آزمایشی"]["وضعیت"], "فقط مشاهده")
        self.now += timedelta(seconds=60)
        self.assertEqual(self.report()["گردآوری"]["علت"], "persistent_capture_rate_limit")
        self.now += timedelta(seconds=60)
        second = self.report()
        self.assertEqual(second["گردآوری"]["علت"], "duplicate body SHA-256")
        self.assertEqual(second["مشاهدات و غربال پژوهشی"]["تعداد ثبت روز"], 1)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(pilot.status(self.config)["وضعیت"], "ok")

    def test_failed_http_attempt_is_reserved_and_stale_health_is_detected(self):
        self.fetch.side_effect = OSError("offline fixture transport failed")
        result = self.report()
        self.assertEqual(result["گردآوری"]["علت"], "offline fixture transport failed")
        self.assertEqual(pilot.status(self.config)["وضعیت"], "degraded")
        self.now += timedelta(seconds=10)
        self.assertEqual(self.report()["گردآوری"]["علت"], "persistent_capture_rate_limit")
        self.fetch.assert_called_once()
        self.now += timedelta(minutes=4)
        self.assertEqual(pilot.status(self.config)["وضعیت"], "گزارش قدیمی یا ناقص")

    def test_observation_backup_restores_seals_report_and_attempt_reservation(self):
        def seal_heads(directory):
            stream = directory / "observations"
            with collector.locked(stream, exclusive=False):
                return [row["head_sha256"] for row in READ_STREAM(stream, collector.SYNTHETIC)]

        self.report()
        self.now += timedelta(seconds=120)
        self.fetch.side_effect = lambda **_: (body(101), self.now, {})
        self.report()
        expected_heads = seal_heads(self.output)
        self.assertEqual(len(expected_heads), 2)
        report_path = self.output / "reports" / "2026-11-22.json"
        report_bytes = report_path.read_bytes()
        backup = self.root / "backup"
        copytree(self.output, backup)
        self.assertEqual(seal_heads(backup), expected_heads)

        damaged = self.root / "damaged"
        copytree(backup, damaged)
        payload = damaged / "observations" / "00000002" / "body.raw.json"
        payload.unlink()
        payload.write_bytes(body(102))
        with self.assertRaises(watch.ArchiveError):
            seal_heads(damaged)

        self.output.rename(self.root / "original")
        copytree(backup, self.output)
        self.assertEqual(seal_heads(self.output), expected_heads)
        self.assertEqual(report_path.read_bytes(), report_bytes)
        self.assertEqual(pilot.status(self.config)["وضعیت"], "ok")
        self.now += timedelta(seconds=30)
        result = self.report()
        self.assertEqual(result["گردآوری"]["علت"], "persistent_capture_rate_limit")
        self.assertEqual(result["مشاهدات و غربال پژوهشی"]["تعداد ثبت روز"], 2)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(seal_heads(self.output), expected_heads)

    def test_unsigned_observations_cannot_open_paper_positions(self):
        self.enable_paper()
        report = self.report()
        self.assertIn("missing: feed", report["دفتر آزمایشی"]["مانع"])
        self.assertEqual(self.ledger.history(), [])
        self.sign(1, entries=[("signed-entry", 1)])
        signed = self.report()["دفتر آزمایشی"]
        self.assertIsNone(signed["مانع"])
        self.assertEqual(signed["موقعیت‌های ربات"][0]["ظرفیت فروش هر سفارش"], 1)
        self.assertEqual(signed["حدود و ظرفیت خرید"]["ظرفیت باقیماندهٔ خرید امروز"], 5000)
        self.assertIn("already processed", self.report()["دفتر آزمایشی"]["مانع"])
        self.assertEqual(len(self.ledger.history()), 1)
        self.assertTrue((self.output / "trusted-batches" / "0000000000000000001.json").is_file())

    def test_bad_signature_stale_quote_and_non_session_inputs_are_rejected(self):
        self.enable_paper()
        self.now = self.now.replace(hour=5, minute=31)
        self.sign(1, entries=[("rejected", 1)])
        envelope = pilot.private_json(self.paths["feed"])
        self.write(self.paths["feed"], dict(envelope, hmac_sha256="0" * 64))
        self.assertIn("signature", self.report()["دفتر آزمایشی"]["مانع"])
        self.sign(1, entries=[("rejected", 1)], quote_changes={
            "quoted_at": (self.now - timedelta(minutes=6)).isoformat()
        })
        self.assertIn("stale quote", self.report()["دفتر آزمایشی"]["مانع"])
        self.sign(1, entries=[("rejected", 1)], quote_changes={
            "quoted_at": (self.now - timedelta(minutes=2)).isoformat()
        })
        self.assertIn("current Tehran session", self.report()["دفتر آزمایشی"]["مانع"])
        self.assertEqual(self.ledger.history(), [])
        self.now = self.now.replace(hour=12, minute=0)
        self.sign(1, entries=[("off-session", 1)])
        self.assertIn("current Tehran session", self.report()["دفتر آزمایشی"]["مانع"])

    def test_due_position_waits_for_signed_quote_and_expires_without_settlement(self):
        self.enable_paper()
        self.sign(1, entries=[("one", 1), ("two", 2)])
        self.report()
        self.now += timedelta(days=7)
        self.paths["feed"].unlink()
        report = self.report()["دفتر آزمایشی"]
        self.assertEqual([p["وضعیت"] for p in report["موقعیت‌های ربات"]], ["EXIT_PENDING"] * 2)
        self.assertEqual(len([row for row in report["هشدارهای بحرانی دفتر"]
                              if row["incident_key"].startswith("failed_exit:")]), 2)
        self.sign(2)
        self.report()
        one, two = self.ledger.history()
        self.assertEqual((one["status"], two["status"], two["remaining"]), ("CLOSED", "EXIT_PENDING", 2))
        self.now = datetime(2026, 12, 22, 6, tzinfo=timezone.utc)
        self.sign(3)
        self.report()
        pending = self.ledger.history()[1]
        self.assertEqual(pending["status"], "EXPIRY_REVIEW")
        self.assertIsNone(pending["realized_pnl"])

    def test_corrupt_archive_blocks_even_signed_orders_without_overwriting_evidence(self):
        self.enable_paper()
        self.report()
        archived = self.output / "observations" / "00000001" / "body.raw.json"
        archived.chmod(0o600)
        archived.write_bytes(b"corrupted input")
        self.sign(1, entries=[("must-not-execute", 1)])
        result = self.report()
        self.assertIsNone(result["مشاهدات و غربال پژوهشی"])
        self.assertIn("SHA-256", result["هشدارهای عملیاتی"][0]["علت"])
        self.assertEqual(archived.read_bytes(), b"corrupted input")
        self.assertEqual(self.ledger.history(), [])

    def test_restart_after_fill_before_watermark_does_not_duplicate_order(self):
        self.enable_paper()
        self.sign(2, entries=[("idempotent", 1)])
        atomic = pilot.atomic_json

        def crash(path, data):
            if path.name == "state.json" and data["last_sequence"] == 2:
                raise OSError("fixture power loss")
            return atomic(path, data)

        with patch("shadow_pilot.atomic_json", side_effect=crash), self.assertRaisesRegex(OSError, "power loss"):
            self.report()
        self.assertEqual(len(self.ledger.history()), 1)
        self.sign(1, entries=[("older", 1)])
        self.assertIn("older", self.report()["دفتر آزمایشی"]["مانع"])
        self.sign(2, entries=[("idempotent", 1)])
        self.report()
        self.assertEqual(len(self.ledger.history()), 1)
        self.assertEqual(pilot.load_state(self.output / "state.json")["last_sequence"], 2)

    def test_paper_clock_is_rechecked_after_scan_before_a_fill(self):
        self.enable_paper()
        self.now = self.now.replace(hour=8, minute=59, second=59)
        self.sign(1, entries=[("session-crossed", 1)])
        original = pilot.PaperMonitor.cycle

        def cross_session(monitor, records):
            if records:
                self.now += timedelta(seconds=1)
            return original(monitor, records)

        with patch.object(pilot.PaperMonitor, "cycle", autospec=True, side_effect=cross_session):
            result = self.report()
        self.assertIn("session ended", result["دفتر آزمایشی"]["مانع"])
        self.assertEqual(self.ledger.history(), [])

    def test_pilot_report_and_monitor_preserve_other_strategy_positions(self):
        self.enable_paper()
        other = PaperTrading(self.paths["db"], clock=lambda: self.now,
                             catalog=load_catalog(self.contracts),
                             sessions=pilot.schedule.load_calendar(self.calendar)[0],
                             policy=replace(self.policy, strategy_id="foreign"))
        self.sign(1)
        data = pilot.private_json(self.paths["feed"])["payload"]["quotes"][0]
        other.buy(data, 1, order_id="foreign")
        before = other.history()
        self.now += timedelta(days=7)
        self.sign(2)
        report = self.report()["دفتر آزمایشی"]
        self.assertEqual(report["موقعیت‌های ربات"], [])
        self.assertEqual(report["خروج‌های آزمایشی امروز"], [])
        self.assertEqual(other.history()[0]["last_quote"], before[0]["last_quote"])
        self.assertIsNone(other.history()[0]["exit_pending_at"])

    def test_missing_state_with_prior_reports_fails_closed(self):
        self.report()
        (self.output / "state.json").unlink()
        with self.assertRaisesRegex(ValueError, "state is missing"):
            self.report()
        self.fetch.assert_called_once()

    def test_corrupt_state_and_concurrent_timer_fail_before_network(self):
        self.output.mkdir(mode=0o700)
        with collector.locked(self.output, exclusive=True), self.assertRaisesRegex(watch.ArchiveError, "busy"):
            self.report()
        self.write(self.output / "state.json", {"last_attempt": "unsafe"})
        with self.assertRaisesRegex(ValueError, "corrupt pilot state"):
            self.report()
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
