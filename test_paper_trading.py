"""Offline tests; the older market smoke scripts are not imported."""

import io
import json
import signal
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime, time, timedelta, timezone
from itertools import count
from pathlib import Path
from unittest.mock import patch

import app
from paper_limits import BotPolicy, PaperLimits
from paper_monitor import PaperMonitor
from paper_quotes import load_catalog
from paper_replay import replay
from paper_trading import SCHEMA, PaperTrading
from trade_manager import main


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "paper.db"
        self.now = datetime(2026, 11, 21, 7, tzinfo=timezone.utc)
        self.contracts = [{
            "ins_code": code, "symbol": "نماد نمونه", "option_type": kind,
            "contract_size": 10, "source": "test-provider",
            "expires_at": (self.now + timedelta(days=30)).isoformat(),
            "first_trading_date": "2026-11-15",
            "last_trading_date": "2026-12-21",
        } for code, kind in (("101", "CALL"), ("202", "PUT"))]
        self.sessions = {f"2026-11-{day}": [(time(9), time(10))] for day in (15, 16, 17, 18, 21)}
        self.catalog = load_catalog(self.contracts)
        self.policy = BotPolicy("test-bot", "test-strategy", PaperLimits(
            100, 100_000, 10_000, 5_000_000, 100_000, 100, 100_000, 1, 100_000,
        ))
        self.ledger = self.reopen()
        self.ledger.initialize(100_000)
        self.ids = count()

    def reopen(self):
        return PaperTrading(self.path, clock=lambda: self.now, catalog=self.catalog, sessions=self.sessions, policy=self.policy)

    def buy(self, quote, quantity):
        return self.ledger.buy(quote, quantity, order_id=f"test-{next(self.ids)}")

    def sell(self, position_id, quote):
        return self.ledger.sell(position_id, quote, order_id=f"sale-{next(self.ids)}")

    def quote(self, **values):
        return {
            "ins_code": "101",
            "symbol": "نماد نمونه",
            "option_type": "CALL",
            "contract_size": 10,
            "best_bid": 99,
            "best_ask": 100,
            "quoted_at": self.now.isoformat(),
            "received_at": self.now.isoformat(),
            "quote_class": "provider",
            "source": "test-provider",
            **values,
        }

    def test_call_put_fill_account_and_persistent_history(self):
        call = self.buy(self.quote(), 2)
        put_quote = self.quote(ins_code="202", option_type="PUT")
        put = self.buy(put_quote, 3)
        self.assertEqual((call["entry_price"], put["entry_price"]), (100, 100))
        self.assertEqual(put["option_type"], "PUT")
        self.assertEqual(self.ledger.status()["unrealized_pnl"], -50)
        self.assertEqual(self.ledger.status()["cash"], 95_000)

        self.now += timedelta(seconds=1)
        close = self.sell(put["id"], self.quote(
            ins_code="202", option_type="PUT", best_bid=110, best_ask=112
        ))
        self.assertEqual((close["exit_price"], close["realized_pnl"]), (110, 300))
        self.assertEqual(close["exit_reason"], "manual")
        with self.assertRaisesRegex(ValueError, "not found"):
            self.sell(put["id"], put_quote)
        reopened = self.reopen()
        status = reopened.status()
        self.assertEqual((status["cash"], status["realized_pnl"], status["allocated"]),
                         (98_300, 300, 2_000))
        self.assertEqual(status["positions"][0]["id"], call["id"])
        self.assertEqual(reopened.history(), [call, close])
        with self.assertRaisesRegex(ValueError, "already initialized"):
            reopened.initialize(1_000_000)

    def test_aggregate_cap_uses_initial_equity_and_contract_multiplier(self):
        first = self.buy(self.quote(), 6)
        self.buy(self.quote(ins_code="202", option_type="PUT"), 4)
        before = self.ledger.status()
        self.assertEqual(before["allocated"], 10_000)
        with self.assertRaisesRegex(ValueError, "10%"):
            self.buy(self.quote(), 1)
        self.assertEqual(self.ledger.status(), before)
        self.now += timedelta(seconds=1)
        self.sell(first["id"], self.quote(best_bid=200, best_ask=201))
        self.assertEqual(self.ledger.status()["realized_pnl"], 6_000)
        with self.assertRaisesRegex(ValueError, "10%"):
            self.buy(self.quote(best_ask=101), 6)
        self.buy(self.quote(), 6)
        self.assertEqual(self.ledger.status()["allocated"], 10_000)

    def test_concurrent_buys_cannot_exceed_cap(self):
        def buy():
            try:
                return self.buy(self.quote(), 6)["status"]
            except ValueError as error:
                self.assertIn("10%", str(error))
                return "REJECTED"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(lambda _: buy(), range(2)), ["OPEN", "REJECTED"])
        self.assertEqual(self.ledger.status()["allocated"], 6_000)

    def test_invalid_quotes_are_rejected_on_buy_mark_and_sell(self):
        pos = self.buy(self.quote(), 1)
        before = self.ledger.status()
        invalid = (
            (self.quote(best_bid=None), "best_bid"),
            (self.quote(best_ask=float("nan")), "best_ask"),
            (self.quote(best_bid=0), "best_bid"),
            (self.quote(best_ask=True), "best_ask"),
            (self.quote(best_bid=101), "crossed"),
            (self.quote(contract_size=0), "contract_size"),
            (self.quote(option_type="SHORT"), "option_type"),
            (self.quote(quoted_at=None, snapshot_received_at=self.now.isoformat()), "time zone"),
            (self.quote(quoted_at=self.now.replace(tzinfo=None).isoformat()), "time zone"),
            (self.quote(quoted_at=(self.now + timedelta(seconds=1)).isoformat()), "future"),
            (self.quote(quoted_at=(self.now - timedelta(minutes=5, seconds=1)).isoformat()), "stale"),
        )
        for quote, error in invalid:
            with self.subTest(error=error, quote=quote):
                for operation in (
                    lambda: self.buy(quote, 1),
                    lambda: self.ledger.mark(pos["id"], quote),
                    lambda: self.sell(pos["id"], quote),
                ):
                    with self.assertRaisesRegex(ValueError, error):
                        operation()
        self.assertEqual(self.ledger.status(), before)
        self.assertEqual(len(self.ledger.history()), 1)

    def test_freshness_boundary_identity_and_out_of_order_quotes(self):
        old = self.quote(quoted_at=(self.now - timedelta(minutes=5)).isoformat())
        pos = self.buy(old, 1)
        self.ledger.mark(pos["id"], self.quote())
        with self.assertRaisesRegex(ValueError, "older"):
            self.sell(pos["id"], old)
        for change in ({"ins_code": "202"}, {"option_type": "PUT"}, {"contract_size": 11}):
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.sell(pos["id"], self.quote(**change))

    def test_stop_alert_is_configurable_persisted_and_does_not_sell(self):
        pos = self.buy(self.quote(), 1)
        self.now += timedelta(seconds=1)
        self.assertFalse(self.ledger.mark(pos["id"], self.quote(best_bid=98))["stop_loss_alert"])
        self.now += timedelta(seconds=1)
        alert = self.ledger.mark(pos["id"], self.quote(best_bid=97))
        self.assertTrue(alert["stop_loss_alert"])
        self.assertEqual(alert["status"], "OPEN")
        self.assertEqual(alert["unrealized_pnl"], -30)
        self.assertEqual(self.reopen().status()["positions"], [alert])
        self.now += timedelta(minutes=5, seconds=1)
        status = self.ledger.status()
        self.assertIsNone(status["unrealized_pnl"])
        self.assertIsNone(status["positions"][0]["stop_loss_alert"])

        custom = PaperTrading(self.path.with_name("other.db"), clock=lambda: self.now,
                              catalog=self.catalog, sessions=self.sessions, policy=self.policy)
        custom.initialize(100_000, stop_loss_bps=100)
        self.assertTrue(custom.buy(self.quote(), 1, order_id="custom")["stop_loss_alert"])
        self.assertEqual(custom.status()["stop_loss_bps"], 100)

    def test_seven_days_includes_weekend_and_never_uses_stale_exit_quote(self):
        pos = self.buy(self.quote(), 1)
        monitor = PaperMonitor(self.ledger)
        self.now += timedelta(days=7, seconds=-1)
        self.assertEqual(self.ledger.mark(pos["id"], self.quote())["status"], "OPEN")
        self.now += timedelta(seconds=1)
        self.assertEqual(self.ledger.status()["positions"][0]["status"], "EXIT_PENDING")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.ledger.mark(pos["id"], self.quote(quoted_at=pos["entry_quote_at"]))
        monitor.cycle(None)
        self.assertEqual(self.ledger.status()["allocated"], 1_000)
        with self.assertRaisesRegex(ValueError, "overdue"):
            self.buy(self.quote(), 1)
        self.assertEqual(self.ledger.mark(pos["id"], self.quote())["status"], "EXIT_PENDING")
        self.now += timedelta(days=1)
        PaperMonitor(self.reopen()).cycle([self.quote(best_bid=90)])
        close, = self.ledger.history()
        self.assertEqual((close["status"], close["exit_reason"], close["exit_price"]),
                         ("CLOSED", "holding_limit", 90))
        self.assertEqual(close["closed_at"], self.now.isoformat())
        self.assertEqual(close["exit_late_us"], 86_400_000_000)
        self.assertEqual(self.ledger.status()["allocated"], 0)
        self.assertEqual(self.ledger.status()["realized_pnl"], -100)

    def test_entry_waits_for_fifth_session_close_in_tehran(self):
        self.sessions["2026-11-21"] = [(time(9), time(9, 15)), (time(9, 30), time(10))]
        self.now = self.now.replace(hour=6, minute=29, second=59)
        with self.assertRaisesRegex(ValueError, "five trading sessions"):
            self.buy(self.quote(), 1)
        self.now += timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "five trading sessions"):
            self.buy(self.quote(quoted_at=(self.now - timedelta(seconds=1)).isoformat()), 1)
        self.assertEqual(self.buy(self.quote(), 1)["status"], "OPEN")

    def test_entry_requires_calendar_and_verified_trading_dates(self):
        for field in ("first_trading_date", "last_trading_date"):
            with self.subTest(field=field):
                contract = self.contracts[0].copy()
                del contract[field]
                self.catalog = load_catalog([contract])
                self.ledger = self.reopen()
                with self.assertRaisesRegex(ValueError, "session calendar"):
                    self.buy(self.quote(), 1)
        self.catalog = load_catalog(self.contracts)
        self.ledger = PaperTrading(self.path, clock=lambda: self.now, catalog=self.catalog, policy=self.policy)
        with self.assertRaisesRegex(ValueError, "session calendar"):
            self.buy(self.quote(), 1)

    def test_earlier_tehran_deadline_waits_for_valid_bid(self):
        self.contracts[0]["last_trading_date"] = "2026-12-04"
        self.catalog = load_catalog(self.contracts)
        self.ledger = self.reopen()
        pos = self.buy(self.quote(), 1)
        deadline = datetime(2026, 11, 23, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(pos["exit_due_at"], deadline.isoformat())
        monitor = PaperMonitor(self.ledger)
        self.now = deadline - timedelta(seconds=1)
        monitor.cycle([self.quote()])
        self.assertEqual(self.ledger.history()[0]["status"], "OPEN")
        self.now = deadline
        monitor.cycle([self.quote(best_bid=0)])
        self.assertEqual(self.ledger.history()[0]["status"], "EXIT_PENDING")
        self.assertEqual(self.ledger.status()["allocated"], 1000)
        monitor.cycle([self.quote(best_bid=90)])
        self.assertEqual(self.ledger.history()[0]["exit_price"], 90)
        with self.assertRaisesRegex(ValueError, "entry cutoff"):
            self.buy(self.quote(), 1)

    def test_existing_position_deadline_can_only_be_tightened(self):
        self.buy(self.quote(), 1)
        self.contracts[0]["last_trading_date"] = "2026-12-04"
        self.catalog = load_catalog(self.contracts)
        ledger = self.reopen()
        self.now = datetime(2026, 11, 23, 20, 30, tzinfo=timezone.utc)
        PaperMonitor(ledger).cycle(None)
        without_catalog = PaperTrading(self.path, clock=lambda: self.now)
        self.assertEqual(without_catalog.history()[0]["status"], "EXIT_PENDING")
        self.assertEqual(without_catalog.history()[0]["exit_due_at"], self.now.isoformat())

    def test_cli_uses_local_quote_and_nonzero_error_status(self):
        quote_file = self.path.with_name("quote.json")
        catalog_file = self.path.with_name("catalog.json")
        quote_file.write_text(json.dumps(self.quote()), encoding="utf-8")
        catalog_file.write_text(json.dumps(self.contracts), encoding="utf-8")
        calendar_file = self.path.with_name("sessions.json")
        calendar_file.write_text(json.dumps({
            "source": "test calendar",
            "sessions": {day: ["09:00-10:00"] for day in self.sessions},
        }), encoding="utf-8")
        policy_file = self.path.with_name("policy.json")
        policy_file.write_text(json.dumps(asdict(self.policy)), encoding="utf-8")
        command = ["paper", "--policy", str(policy_file), "--db", str(self.path), "--catalog", str(catalog_file),
                   "buy", "--calendar", str(calendar_file), "--quantity", "1", "--quote", str(quote_file), "--order-id", "cli"]
        with patch("trade_manager.PaperTrading", return_value=self.ledger) as build:
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(command), 0)
            self.assertEqual(json.loads(out.getvalue())["status"], "OPEN")
            build.assert_called_once_with(self.path, catalog=self.catalog, sessions=self.sessions, policy=self.policy)
            self.now += timedelta(minutes=6)
            command[-1] = "cli-new"
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(command), 1)
            self.assertIn("stale quote", err.getvalue())

    def test_duplicate_order_id_is_atomic_across_concurrent_connections(self):
        quote = self.quote()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda _: self.reopen().buy(quote, 2, order_id="retry"), range(8)
            ))
        self.assertEqual({result["id"] for result in results}, {1})
        self.assertEqual(self.ledger.status()["cash"], 98_000)
        self.assertEqual(len(self.ledger.history()), 1)
        with self.assertRaisesRegex(ValueError, "different request"):
            self.ledger.buy(quote, 3, order_id="retry")
        self.now += timedelta(minutes=6)
        self.assertEqual(self.ledger.buy(quote, 2, order_id="retry")["id"], 1)

    def test_provenance_offsets_and_processing_timestamps(self):
        original = self.now
        self.now = datetime(2026, 11, 21, 21, 0, tzinfo=timezone.utc)
        tehran = timezone(timedelta(hours=3, minutes=30))
        at = self.now - timedelta(minutes=5)
        local_quote = self.quote(
            quoted_at=at.astimezone(tehran).isoformat(),
            received_at=(self.now - timedelta(seconds=2)).isoformat(),
        )
        pos = self.buy(local_quote, 1)
        self.assertEqual(pos["entry_quote"]["quoted_at"], at.isoformat())
        self.assertEqual(pos["entry_quote"]["received_at"], local_quote["received_at"])
        self.assertEqual(pos["entry_quote"]["processed_at"], self.now.isoformat())
        self.assertEqual(pos["exit_due_at"], (self.now + timedelta(days=7)).isoformat())
        for changes, error in (
            ({"quote_class": "manual"}, "manual or unverifiable"),
            ({"quote_class": None}, "manual or unverifiable"),
            ({"source": "unapproved"}, "untrusted source"),
            ({"contract_size": 1}, "multiplier"),
            ({"received_at": None}, "time zone"),
            ({"received_at": (self.now - timedelta(seconds=1)).isoformat()}, "later than"),
            ({"received_at": (self.now + timedelta(seconds=1)).isoformat()}, "future"),
        ):
            with self.subTest(error=error, changes=changes), self.assertRaisesRegex(ValueError, error):
                self.buy(self.quote(**changes), 1)
        self.assertNotEqual(original.date(), self.now.astimezone(tehran).date())

    def test_monitor_repeated_cycles_recover_with_one_exit_and_incident(self):
        pos = self.buy(self.quote(), 1)
        self.now += timedelta(days=7, microseconds=40)
        monitor = PaperMonitor(self.ledger, 10)
        stale = self.quote(quoted_at=pos["entry_quote_at"])
        for records in (None, [], [stale], [stale]):
            monitor.cycle(records)
        self.assertEqual(self.ledger.status()["cash"], 99_000)
        self.assertEqual(monitor.status()["effective_state"], "running")
        self.assertEqual([row["incident_key"] for row in monitor.incidents()],
                         ["feed", "holding:1", "quote:1", "failed_exit:1"])
        self.now += timedelta(seconds=26)
        self.assertEqual(monitor.status()["effective_state"], "unresponsive")
        restarted = PaperMonitor(self.reopen(), 10)
        older = (self.now - timedelta(seconds=10)).isoformat()
        quotes = [self.quote(best_bid=87, quoted_at=older), self.quote(best_bid=90)]
        restarted.cycle(quotes)
        restarted.cycle(quotes)
        close, = self.ledger.history()
        self.assertEqual(close["exit_late_us"], 26_000_040)
        self.assertEqual(close["exit_price"], 90)
        self.assertEqual(self.ledger.status()["cash"], 99_900)
        self.assertTrue(all(row["resolved_at"] for row in restarted.incidents()))
        self.assertEqual(sum(row["incident_key"] == "heartbeat_gap" for row in restarted.incidents()), 1)
        restarted.stop()
        self.assertEqual(restarted.status()["effective_state"], "stopped")
        self.assertEqual(restarted.status()["last_success_at"], self.now.isoformat())

    def test_same_timestamp_conflict_does_not_close_overdue_position(self):
        self.buy(self.quote(), 1)
        self.now += timedelta(days=7)
        PaperMonitor(self.ledger).cycle([self.quote(best_bid=98), self.quote(best_bid=99)])
        self.assertEqual(self.ledger.history()[0]["status"], "EXIT_PENDING")

    def test_expiry_is_not_a_sale_or_a_zero_valuation(self):
        pos = self.buy(self.quote(), 1)
        self.now += timedelta(days=30)
        monitor = PaperMonitor(self.ledger)
        monitor.cycle([self.quote()])
        summary = self.ledger.status()
        self.assertEqual(summary["positions"][0]["status"], "EXPIRY_REVIEW")
        self.assertEqual(summary["allocated"], 1_000)
        self.assertIsNone(summary["unrealized_pnl"])
        self.assertEqual(summary["realized_pnl"], 0)
        self.assertIn("expiry:1", [row["incident_key"] for row in monitor.incidents()])
        with self.assertRaisesRegex(ValueError, "settlement"):
            self.sell(pos["id"], self.quote())
        with self.assertRaisesRegex(ValueError, "expired"):
            self.buy(self.quote(ins_code="202", option_type="PUT"), 1)

    def test_final_tehran_date_boundary_blocks_manual_and_monitor_fills(self):
        self.contracts[0]["last_trading_date"] = "2026-12-04"
        self.catalog = load_catalog(self.contracts)
        self.ledger = self.reopen()
        pos = self.buy(self.quote(), 1)
        monitor = PaperMonitor(self.ledger)
        self.now = datetime(2026, 12, 4, 20, 29, 59, tzinfo=timezone.utc)
        monitor.cycle([self.quote(quote_class="synthetic"), self.quote(best_bid=0)])
        before = self.ledger.history()[0]
        self.assertEqual(before["status"], "EXIT_PENDING")
        self.assertIsNone(before["realized_pnl"])

        cutoff = self.now + timedelta(seconds=1)
        self.now = cutoff
        self.assertEqual(self.ledger.history()[0]["status"], "EXPIRY_REVIEW")
        previous_quote = self.quote(quoted_at=(cutoff - timedelta(seconds=1)).isoformat())
        with self.assertRaisesRegex(ValueError, "official settlement"):
            self.sell(pos["id"], previous_quote)
        monitor.cycle([previous_quote, self.quote()])
        position, = self.reopen().history()
        self.assertEqual(position["status"], "EXPIRY_REVIEW")
        self.assertEqual(position["expiry_seen_at"], cutoff.isoformat())
        self.assertEqual(position["last_quote"], before["last_quote"])
        for field in ("closed_at", "exit_price", "exit_reason", "realized_pnl", "unrealized_pnl"):
            self.assertIsNone(position[field])
        summary = self.ledger.status()
        self.assertEqual((summary["cash"], summary["allocated"], summary["realized_pnl"]), (99_000, 1000, 0))

        # A later catalog date and a clock rollback cannot reopen a settlement case.
        self.catalog = load_catalog([dict(self.contracts[0], last_trading_date="2026-12-21")])
        self.now -= timedelta(seconds=1)
        restarted = self.reopen()
        PaperMonitor(restarted).cycle([self.quote()])
        self.assertEqual(restarted.history()[0]["status"], "EXPIRY_REVIEW")

    def test_valid_quote_on_final_date_can_close_before_cutoff(self):
        self.contracts[0]["last_trading_date"] = "2026-12-04"
        self.catalog = load_catalog(self.contracts)
        self.ledger = self.reopen()
        self.buy(self.quote(), 1)
        self.now = datetime(2026, 12, 4, 8, tzinfo=timezone.utc)
        PaperMonitor(self.ledger).cycle([self.quote(best_bid=105, best_ask=106)])
        restarted = PaperTrading(self.path, clock=lambda: self.now)
        position, = restarted.history()
        self.assertEqual((position["status"], position["realized_pnl"]), ("CLOSED", 50))

    def test_legacy_position_without_verified_final_date_stays_pending(self):
        pos = self.buy(self.quote(), 1)
        # Emulate the schema at ebab9fb.
        with self.ledger.transaction() as db:
            db.execute("ALTER TABLE positions DROP COLUMN trade_cutoff_at")
        contract = self.contracts[0].copy()
        del contract["last_trading_date"]
        self.catalog = load_catalog([contract])
        self.now += timedelta(days=7)
        restarted = self.reopen()
        self.assertEqual(restarted.history()[0]["status"], "EXIT_PENDING")
        with self.assertRaisesRegex(ValueError, "final trading date"):
            restarted.sell(pos["id"], self.quote(), order_id="unverified")
        PaperMonitor(restarted).cycle([self.quote()])
        position, = restarted.history()
        self.assertIsNone(position["exit_price"])
        self.assertIsNone(position["realized_pnl"])
        self.assertIsNotNone(position["exit_pending_at"])

    def test_legacy_closed_rows_without_executable_evidence_hide_realized_pnl(self):
        pos = self.buy(self.quote(), 1)
        self.now += timedelta(days=7)
        closed = self.sell(pos["id"], self.quote(best_bid=90))
        self.assertEqual(closed["realized_pnl"], -100)
        for changes in (
            {"last_quote": None},
            {"last_quote": json.dumps(dict(closed["last_quote"], quote_class="synthetic"))},
            {"trade_cutoff_at": closed["closed_at"]},
        ):
            with self.subTest(changes=changes):
                with self.ledger.transaction() as db:
                    db.execute("UPDATE positions SET last_quote = ?, trade_cutoff_at = ?",
                               (json.dumps(closed["last_quote"]), closed["trade_cutoff_at"]))
                    db.execute("UPDATE positions SET " + ", ".join(f"{key} = ?" for key in changes),
                               tuple(changes.values()))
                self.assertIsNone(self.ledger.history()[0]["realized_pnl"])
                self.assertIsNone(self.ledger.status()["realized_pnl"])
                self.assertEqual(self.ledger.status()["cash"], 99_900)

    def test_emergency_pause_persists_and_does_not_block_exit(self):
        pos = self.buy(self.quote(), 1)
        self.ledger.set_pause(True)
        self.ledger = self.reopen()
        self.assertTrue(self.ledger.status()["entries_paused"])
        with self.assertRaisesRegex(ValueError, "paused"):
            self.buy(self.quote(), 1)
        self.sell(pos["id"], self.quote())
        self.ledger.set_pause(False)
        self.assertEqual(self.buy(self.quote(), 1)["status"], "OPEN")

    def test_write_error_rolls_back_exit_and_monitor_recovers_after_restart(self):
        pos = self.buy(self.quote(), 1)
        self.now += timedelta(days=7)
        with self.ledger.transaction() as db:
            db.execute("""CREATE TRIGGER test_fail_cash BEFORE UPDATE OF cash ON account
                BEGIN SELECT RAISE(ABORT, 'forced rollback'); END""")
        monitor = PaperMonitor(self.ledger)
        with self.assertRaises(sqlite3.IntegrityError):
            monitor.cycle([self.quote(best_bid=90)])
        monitor.failure()
        pending, = self.reopen().history()
        self.assertIsNone(pending["exit_pending_at"])
        self.assertIsNone(pending["closed_at"])
        self.assertEqual(pending["last_quote"], pos["last_quote"])
        self.assertEqual(self.ledger.status()["cash"], 99_000)
        self.assertEqual(monitor.status()["effective_state"], "error")
        with self.ledger.transaction() as db:
            db.execute("DROP TRIGGER test_fail_cash")
        PaperMonitor(self.reopen()).cycle([self.quote(best_bid=90)])
        self.assertEqual(self.ledger.status()["realized_pnl"], -100)
        self.assertEqual(self.ledger.status()["cash"], 99_900)

    def test_insufficient_cash_prevents_negative_cash_even_below_allocation_cap(self):
        for _ in range(10):
            pos = self.buy(self.quote(best_ask=1000), 1)
            self.now += timedelta(seconds=1)
            self.sell(pos["id"], self.quote(best_bid=90))
            self.now += timedelta(seconds=1)
        self.assertEqual(self.ledger.status()["cash"], 9_000)
        self.assertEqual(self.ledger.status()["allocated"], 0)
        with self.assertRaisesRegex(ValueError, "insufficient paper cash"):
            self.buy(self.quote(best_ask=1000), 1)
        self.assertEqual(self.ledger.status()["cash"], 9_000)

    def test_migration_keeps_legacy_ledger_but_does_not_trust_historical_mark(self):
        self.path = self.path.with_name("v1.db")
        with closing(sqlite3.connect(self.path)) as db, db:
            for sql in SCHEMA:
                db.execute(sql)
            db.execute("INSERT INTO account VALUES (1, 100000, 99000, 300)")
            db.execute("""INSERT INTO positions
                (ins_code, symbol, option_type, quantity, contract_size, entry_price,
                 entry_quote_at, opened_at, exit_due_at, last_bid, last_quote_at)
                VALUES ('101', 'نماد نمونه', 'CALL', 1, 10, 100, ?, ?, ?, 99, ?)""",
                (self.now.isoformat(), self.now.isoformat(),
                 (self.now + timedelta(days=7)).isoformat(), self.now.isoformat()))
        ledger = self.reopen()
        self.assertIsNone(ledger.status()["unrealized_pnl"])
        PaperMonitor(ledger).cycle([self.quote()])
        self.assertIsNone(ledger.status()["unrealized_pnl"])
        with self.assertRaisesRegex(ValueError, "ownership"):
            ledger.sell(1, self.quote(), order_id="legacy-attempt")
        self.assertIsNone(ledger.history()[0]["entry_quote"])

    def test_ownership_is_scoped_to_bot_and_strategy(self):
        pos = self.buy(self.quote(), 2)
        self.now += timedelta(days=7)
        base = self.policy
        before = self.ledger.history()
        for other in (replace(base, bot_id="other"), replace(base, strategy_id="other")):
            self.policy = other
            foreign = self.reopen()
            with self.assertRaisesRegex(ValueError, "ownership"):
                foreign.mark(pos["id"], self.quote())
            with self.assertRaisesRegex(ValueError, "ownership"):
                foreign.sell(pos["id"], self.quote(), order_id="foreign")
            with self.assertRaisesRegex(ValueError, "order_id"):
                foreign.buy(self.quote(), 2, order_id=pos["order_id"])
            PaperMonitor(foreign).cycle([self.quote()])
        self.assertEqual(self.ledger.history(), before)
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_exits").fetchone()[0], 0)
            self.assertIsNone(self.ledger.position(db, pos["id"])["exit_pending_at"])

    def test_buy_limits_are_individually_enforced_and_missing_policy_fails_closed(self):
        original = self.policy
        for limit, maximum, quantity, reason in (
            ("max_buy_quantity", 1, 2, "buy quantity"),
            ("max_buy_value", 999, 1, "buy order value"),
            ("max_buy_price", 99, 1, "buy limit price"),
            ("max_daily_purchase_value", 999, 1, "daily purchase value"),
            ("max_open_exposure", 999, 1, "total open exposure"),
        ):
            with self.subTest(limit=limit):
                self.policy = replace(original, limits=replace(original.limits, **{limit: maximum}))
                self.ledger = self.reopen()
                with self.assertRaisesRegex(ValueError, reason):
                    self.buy(self.quote(), quantity)
        self.assertEqual(self.ledger.history(), [])
        with self.assertRaisesRegex(ValueError, "paper limits"):
            PaperTrading(self.path).buy(self.quote(), 1, order_id="no-policy")
        with self.assertRaises(ValueError):
            BotPolicy.parse({"bot_id": "b", "strategy_id": "s", "limits": {"max_buy_price": 100}})

    def test_partial_exits_are_audited_idempotent_and_cannot_oversell(self):
        pos = self.buy(self.quote(), 3)
        self.now += timedelta(seconds=1)
        quote = self.quote(best_bid=90)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.reopen().sell(
                pos["id"], quote, order_id="partial", quantity=1
            ), range(8)))
        self.assertEqual({result["remaining"] for result in results}, {2})
        self.assertEqual(self.ledger.status()["realized_pnl"], -100)
        self.assertEqual(self.ledger.status()["allocated"], 2000)
        self.assertEqual(self.ledger.status()["cash"], 97_900)
        for order, quantity, reason in (
            ("partial", 2, "different request"), ("oversell", 3, "available bot-owned"),
            (pos["order_id"], 1, "used for a buy"),
        ):
            with self.assertRaisesRegex(ValueError, reason):
                self.reopen().sell(pos["id"], quote, order_id=order, quantity=quantity)
        self.now += timedelta(seconds=1)
        second_quote = self.quote(best_bid=110, best_ask=111)
        closed = self.reopen().sell(pos["id"], second_quote, order_id="final", quantity=2)
        self.assertEqual((closed["status"], closed["remaining"], closed["realized_pnl"]), ("CLOSED", 0, 100))
        self.assertEqual([fill["order_id"] for fill in closed["exit_orders"]], ["partial", "final"])
        self.assertEqual(self.reopen().sell(pos["id"], second_quote, order_id="final", quantity=2), closed)

    def test_required_exit_honors_sell_limits_and_alerts_until_safe(self):
        pos = self.buy(self.quote(), 2)
        self.now += timedelta(days=7)
        original = self.policy
        for limit, value in (("max_sell_quantity", 1), ("max_sell_value", 1000), ("min_sell_price", 95)):
            with self.subTest(limit=limit):
                self.policy = replace(original, limits=replace(original.limits, **{limit: value}))
                ledger = self.reopen()
                monitor = PaperMonitor(ledger)
                monitor.cycle([self.quote(best_bid=90)])
                position, = ledger.history()
                self.assertEqual((position["status"], position["remaining"]), ("EXIT_PENDING", 2))
                self.assertIsNone(position["realized_pnl"])
                alert = next(item for item in monitor.incidents() if item["incident_key"] == "failed_exit:1")
                self.assertEqual((alert["severity"], alert["resolved_at"]), ("critical", None))
        self.policy = original
        ledger = self.reopen()
        ledger.set_pause(True)
        ledger.sell(pos["id"], self.quote(best_bid=90), order_id="recover", quantity=1)
        PaperMonitor(ledger).cycle([self.quote(best_bid=90)])
        self.assertEqual(ledger.history()[0]["remaining"], 0)
        self.assertTrue(ledger.status()["entries_paused"])

    def test_daily_purchase_and_gross_loss_caps_roll_at_tehran_midnight(self):
        self.now = datetime(2026, 11, 21, 20, 29, tzinfo=timezone.utc)
        self.policy = replace(self.policy, limits=replace(
            self.policy.limits, max_daily_purchase_value=1000, max_daily_loss=100,
        ))
        self.ledger = self.reopen()
        pos = self.buy(self.quote(), 1)
        with self.assertRaisesRegex(ValueError, "daily purchase value"):
            self.buy(self.quote(), 1)
        self.now += timedelta(seconds=1)
        self.sell(pos["id"], self.quote(best_bid=90))
        report = self.reopen().status()["گزارش حدود و ظرفیت روزانه (ریال)"]
        self.assertEqual(report["ظرفیت باقیماندهٔ خرید امروز"], 0)
        self.assertEqual(report["ظرفیت باقیماندهٔ زیان امروز"], 0)
        with self.assertRaisesRegex(ValueError, "daily loss"):
            self.buy(self.quote(), 1)
        self.now = datetime(2026, 11, 21, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(self.buy(self.quote(), 1)["status"], "OPEN")

    def test_replay_uses_same_limits_pause_and_provenance_in_isolated_ledger(self):
        replay_policy = replace(self.policy, limits=replace(self.policy.limits, max_buy_quantity=1))
        at = self.now.isoformat()
        events = [
            {"at": at, "action": "buy", "quote": self.quote(), "quantity": 2, "order_id": "too-many"},
            {"at": at, "action": "buy", "quote": self.quote(), "quantity": 1, "order_id": "open"},
            {"at": at, "action": "pause"},
            {"at": at, "action": "buy", "quote": self.quote(), "quantity": 1, "order_id": "paused"},
        ]
        self.now += timedelta(days=7)
        events.append({"at": self.now.isoformat(), "action": "monitor",
                       "quotes": [self.quote(quote_class="synthetic")]})
        result = replay(events, equity=100_000, catalog=self.catalog, sessions=self.sessions, policy=replay_policy)
        self.assertEqual([item["event"] for item in result["رویدادهای ردشده"]], [1, 4])
        self.assertEqual(result["تاریخچه"][0]["status"], "EXIT_PENDING")
        self.assertEqual(self.ledger.history(), [])
        self.assertEqual(self.ledger.status()["cash"], 100_000)

    def lifecycle_report(self):
        contracts = self.contracts + [dict(self.contracts[1], ins_code="303")]
        policy = replace(self.policy, limits=replace(
            self.policy.limits, max_open_exposure=5_000, max_sell_quantity=1,
        ))
        start = self.now
        events = []

        def quote(code, at, *, bid=99, ask=100, **fields):
            return self.quote(ins_code=code, option_type="CALL" if code == "101" else "PUT",
                              quoted_at=at.isoformat(), received_at=at.isoformat(),
                              best_bid=bid, best_ask=ask, **fields)

        def screen(code, quantity, order, *, trades=40, offset=timedelta()):
            at = start + offset
            return {"action": "screen", "at": at.isoformat(), "quote": quote(code, at),
                    "quantity": quantity, "order_id": order, "market": {
                        "observed_at": at.isoformat(), "strike": 1_000,
                        "underlying_closing_price": 1_000, "trade_count": trades,
                        "trade_volume": 100, "bid_volume": 20, "ask_volume": 20,
                    }}

        events.append(screen("101", 1, "before-fifth", offset=timedelta(minutes=-31)))
        events.append(screen("101", 1, "thin", trades=1))
        events.append(screen("101", 2, "call"))
        events.append(screen("202", 2, "put"))
        events.append(screen("303", 1, "put-awaiting"))
        events.append(screen("101", 1, "exposure-cap"))
        next_day = start + timedelta(days=1)
        events.append({"action": "monitor", "at": next_day.isoformat(),
                       "quotes": [quote("101", next_day, bid=94), quote("202", next_day, bid=95)]})
        for position_id, code, bid in ((1, "101", 94), (2, "202", 95)):
            exit_event = {"action": "sell", "at": next_day.isoformat(),
                          "position_id": position_id, "quote": quote(code, next_day, bid=bid),
                          "quantity": 1, "order_id": f"partial-{code}"}
            events.extend([exit_event, exit_event])
        deadline = start + timedelta(days=7)
        events.append({"action": "monitor", "at": deadline.isoformat(),
                       "quotes": [quote("101", deadline, bid=105, ask=106),
                                  quote("202", deadline, bid=110, ask=111),
                                  quote("303", deadline, quote_class="synthetic")]})
        after_final = datetime(2026, 12, 21, 21, tzinfo=timezone.utc)
        events.append({"action": "monitor", "at": after_final.isoformat(), "quotes": []})

        catalog = self.path.with_name("demo-catalog.json")
        limits = self.path.with_name("demo-policy.json")
        calendar = self.path.with_name("demo-sessions.json")
        stream = self.path.with_name("demo-events.json")
        for path, data in ((catalog, contracts), (limits, asdict(policy)), (stream, events),
                           (calendar, {"source": "synthetic test calendar", "sessions": {
                               day: ["09:00-10:00"] for day in self.sessions
                           }})):
            path.write_text(json.dumps(data), encoding="utf-8")
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            status = main(["paper", "--db", str(self.path), "--catalog", str(catalog),
                           "--policy", str(limits), "replay", "--calendar", str(calendar),
                           "--equity", "100000", "--events", str(stream),
                           "--input-class", "synthetic_fixture"])
        self.assertEqual(status, 0, errors.getvalue())
        return output.getvalue()

    def test_deterministic_end_to_end_call_and_put_cli_lifecycle(self):
        raw = self.lifecycle_report()
        self.assertEqual(raw, self.lifecycle_report())
        report = json.loads(raw)
        self.assertEqual(report["ردهٔ ورودی"], "synthetic_fixture")
        self.assertFalse(report["پوشش شواهد واقعی"]["واجد شرایط اجرا"])
        self.assertEqual(report["پوشش شواهد واقعی"]["شاهد اجرایی یا تسویهٔ مستقل تأییدشده در رجیستری"], 0)
        self.assertEqual([item["reason"] for item in report["رویدادهای ردشده"]],
                         ["five trading sessions must be completed before entry",
                          "insufficient_trades", "total open exposure limit exceeded"])
        call, put, awaiting = report["تاریخچه"]
        self.assertEqual([(pos["option_type"], pos["status"], pos["remaining"]) for pos in (call, put, awaiting)],
                         [("CALL", "CLOSED", 0), ("PUT", "CLOSED", 0), ("PUT", "EXPIRY_REVIEW", 1)])
        for pos in (call, put):
            self.assertEqual((pos["bot_id"], pos["strategy_id"]), ("test-bot", "test-strategy"))
            self.assertEqual([fill["quantity"] for fill in pos["exit_orders"]], [1, 1])
            self.assertEqual(pos["exit_reason"], "holding_limit")
        self.assertEqual((call["realized_pnl"], put["realized_pnl"]), (-10, 50))
        self.assertIsNone(awaiting["realized_pnl"])
        self.assertEqual(report["حساب"]["allocated"], 1000)
        self.assertEqual(report["حساب"]["realized_pnl"], 40)
        alerts = {alert["incident_key"] for alert in report["هشدارها"]}
        self.assertTrue({"stop_loss:1", "stop_loss:2", "failed_exit:3", "expiry:3"} <= alerts)
        self.assertEqual(self.ledger.history(), [])

    def test_concurrent_distinct_partial_exits_cannot_oversell(self):
        position = self.buy(self.quote(), 3)
        self.now += timedelta(seconds=1)
        quote = self.quote(best_bid=90)

        def exit_two(order_id):
            try:
                return self.reopen().sell(position["id"], quote, order_id=order_id, quantity=2)
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.map(exit_two, ("distinct-a", "distinct-b"))
        accepted = [item for item in (a, b) if isinstance(item, dict)]
        rejected = [item for item in (a, b) if isinstance(item, str)]
        self.assertEqual([item["remaining"] for item in accepted], [1])
        self.assertEqual(rejected, ["sell quantity exceeds available bot-owned quantity"])
        self.assertEqual(self.reopen().status()["cash"], 98_800)
        self.assertEqual(len(self.reopen().history()[0]["exit_orders"]), 1)

    def test_exit_refuses_account_clock_rollback_after_another_position_trade(self):
        first = self.buy(self.quote(), 1)
        self.now += timedelta(minutes=1)
        self.buy(self.quote(ins_code="202", option_type="PUT"), 1)
        self.now -= timedelta(seconds=30)
        with self.assertRaisesRegex(ValueError, "clock is older than recorded paper activity"):
            self.sell(first["id"], self.quote())
        self.assertEqual(self.reopen().history()[0]["remaining"], 1)

    def test_monitor_and_execution_do_not_open_network_connections(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            pos = self.buy(self.quote(), 1)
            self.now += timedelta(days=7)
            PaperMonitor(self.ledger).cycle([self.quote(best_bid=80)])
        self.assertEqual(self.ledger.history()[0]["id"], pos["id"])
        self.assertEqual(self.ledger.history()[0]["exit_price"], 80)

    def test_monitor_once_uses_local_file_and_releases_single_runner_lock(self):
        self.buy(self.quote(), 1)
        self.now += timedelta(days=7)
        monitor = PaperMonitor(self.ledger)
        feed = self.path.with_name("feed.json")
        feed.write_text(json.dumps([self.quote(best_bid=95)]), encoding="utf-8")
        with monitor._lock(), self.assertRaisesRegex(ValueError, "already running"):
            PaperMonitor(self.reopen()).run(feed, once=True)
        monitor.run(feed, once=True)
        self.assertEqual(monitor.status()["state"], "stopped")
        self.assertEqual(self.ledger.history()[0]["exit_price"], 95)

    def test_sigterm_stops_polling_and_persists_heartbeat(self):
        monitor = PaperMonitor(self.ledger)
        handler = signal.getsignal(signal.SIGTERM)
        with patch("paper_monitor.sleep", side_effect=lambda _: signal.raise_signal(signal.SIGTERM)):
            monitor.run(self.path.with_name("missing.json"))
        health = self.reopen()
        self.assertEqual(PaperMonitor(health).status()["state"], "stopped")
        self.assertEqual(PaperMonitor(health).status()["data_state"], "unavailable")
        self.assertIs(signal.getsignal(signal.SIGTERM), handler)


class ScannerTests(unittest.TestCase):
    def test_scan_keeps_existing_api_shape_and_receipt_time_warning(self):
        data = {"instrumentOptMarketWatch": [{
            "remainedDay": 30, "pMeDem_C": 99, "pMeOf_C": 100, "qTitMeDem_C": 10,
            "qTitMeOf_C": 10, "pDrCotVal_UA": 1000, "strikePrice": 1000,
            "contractSize": 1000, "lVal18AFC_C": "نماد نمونه",
        }]}
        with patch("app.urllib.request.urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = json.dumps(data).encode("utf-8")
            result = app.scan_market()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["puts"], [])
        self.assertEqual((result["calls"][0]["bid"], result["calls"][0]["ask"]), (99, 100))
        self.assertEqual(result["snapshot_status"], "fresh")
        self.assertIn("زمان به‌روزرسانی واقعی", result["snapshot_warning"])


if __name__ == "__main__":
    unittest.main()
