"""Offline tests: the only sockets opened by this module are loopback HTTP."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from os import stat_result
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import approval_dashboard as dashboard
from approval_workflow import ApprovalStore, Proposal, RiskSnapshot
from paper_quotes import Instrument, Quote


class ApprovalFixture(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.db = Path(scratch.name) / "approvals.sqlite3"
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.instrument = Instrument("123", "fixture <script>", "CALL", 100,
                                     "synthetic-test-provider", self.now + timedelta(days=30))
        self.quote = Quote(self.instrument, 99, 100, self.now - timedelta(seconds=20),
                           self.now - timedelta(seconds=10))
        self.rank = dict(ins_code="123", symbol=self.instrument.symbol, option_type="CALL",
                         contract_size=100, remaining_days=30, spread_percent=1, trade_count=50,
                         trade_volume=100, best_bid=99, best_ask=100, bid_volume=7, ask_volume=6,
                         ask_time_value=90, iv_status="ok", greeks_assumption_status="validated", delta=.6)
        self.risk = RiskSnapshot(1000000, 80000, 920000, 300, True)
        self.proposal = self.build()
        self.current = [self.proposal]
        self.store = self.open_store()

    def build(self, *, quantity=1, rank=None, quote=None, risk=None):
        return Proposal.from_verified_inputs(rank if rank is not None else self.rank,
                                             quote if quote is not None else self.quote, quantity,
                                             risk if risk is not None else self.risk,
                                             "synthetic-fixture-only", self.now)

    def open_store(self):
        store = ApprovalStore(self.db, lambda: self.current, clock=lambda: self.now)
        store.initialize()
        store.reconcile()
        return store

    def row(self):
        row, = self.store.recent()
        return row

    def decide(self, decision="APPROVED"):
        row = self.row()
        return self.store.decide(row["id"], row["fingerprint"], decision)

    def audit(self):
        return [(entry["state"], entry["actor"])
                for entry in self.store.detail(self.row()["id"])[1]]


class ApprovalStoreTests(ApprovalFixture):
    def test_approval_restart_and_replay(self):
        old = dict(self.row())
        self.assertEqual(old["state"], "PENDING")
        self.assertEqual(self.decide(), "recorded")
        self.store = self.open_store()
        self.assertEqual(self.decide(), "already_closed")
        self.assertEqual(self.decide("REJECTED"), "already_closed")
        self.assertEqual(dict(self.row()), {**old, "state": "APPROVED"})
        self.assertEqual(self.audit(), [("PENDING", "system"), ("APPROVED", "operator")])

    def test_reject_and_explicit_cancel(self):
        self.assertEqual(self.decide("REJECTED"), "recorded")
        self.assertEqual(self.audit(), [("PENDING", "system"), ("REJECTED", "operator")])
        self.now += timedelta(seconds=2)
        self.quote = replace(self.quote, quoted_at=self.quote.quoted_at + timedelta(seconds=2))
        self.current = [self.build()]
        self.store.reconcile()
        pending = self.store.recent()[0]
        self.assertEqual(self.store.decide(pending["id"], pending["fingerprint"], "CANCELLED"), "recorded")
        self.assertEqual(self.store.detail(pending["id"])[1][-1]["actor"], "operator")

    def test_expiry_at_exact_deadline_and_restart(self):
        self.now = self.proposal.valid_until
        self.assertEqual(self.decide(), "expired")
        self.store = self.open_store()
        self.assertEqual(self.audit(), [("PENDING", "system"), ("EXPIRED", "system")])
        self.assertEqual(self.decide(), "already_closed")

    def test_startup_expires_missed_deadlines(self):
        self.now = self.proposal.valid_until + timedelta(days=1)
        self.store = self.open_store()
        self.assertEqual(self.row()["state"], "EXPIRED")

    def test_changed_or_stale_form_is_not_authorized(self):
        row = self.row()
        self.assertEqual(self.store.decide(row["id"], "0" * 64, "APPROVED"), "wrong_snapshot")
        modified = self.build(quantity=2)
        self.assertEqual(modified.key, self.proposal.key)
        self.assertNotEqual(modified.fingerprint, self.proposal.fingerprint)
        self.current = [modified]
        self.assertEqual(self.decide(), "changed")
        self.store.reconcile()
        self.assertEqual(self.row()["fingerprint"], row["fingerprint"])
        self.assertEqual(self.audit(), [("PENDING", "system"), ("CANCELLED", "system")])

    def test_withdrawn_proposal_and_clock_rollback(self):
        old_now = self.now
        self.now -= timedelta(seconds=1)
        self.assertEqual(self.decide(), "clock_error")
        self.now = old_now
        self.current = []
        self.assertEqual(self.decide(), "changed")
        self.assertEqual(self.row()["state"], "CANCELLED")

    def test_concurrent_decisions_and_duplicate_ingest(self):
        peers = Barrier(8)
        row = self.row()

        def race(i):
            peers.wait()
            self.store.reconcile()
            return self.store.decide(row["id"], row["fingerprint"],
                                     "APPROVED" if i % 2 else "REJECTED")

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(race, range(8)))
        self.assertCountEqual(outcomes, ["recorded", *(["already_closed"] * 7)])
        self.assertEqual(len(self.audit()), 2)

    def test_deadline_sampled_after_provider_evaluation(self):
        def delayed_current():
            self.now = self.proposal.valid_until
            return self.current
        self.store.eligible = delayed_current
        self.assertEqual(self.decide(), "expired")

    def test_constraints(self):
        for params in (
            {"rank": {**self.rank, "greeks_assumption_status": "experimental_unverified"}},
            {"rank": {**self.rank, "best_ask": 101}},
            {"rank": {**self.rank, "trade_count": float("nan")}},
            {"rank": {**self.rank, "best_bid": 90}, "quote": replace(self.quote, bid=90)},
            {"quote": {}},
            {"quote": replace(self.quote, quoted_at=self.now + timedelta(seconds=1))},
            {"quote": replace(self.quote, quoted_at=self.now - timedelta(minutes=6))},
            {"quote": replace(self.quote, instrument=replace(self.instrument, expires_at=self.now))},
            {"quote": replace(self.quote, instrument=replace(self.instrument, expires_at=self.now + timedelta(days=6)))},
            {"quantity": 3}, {"quantity": 7}, {"quantity": 0},
            {"risk": replace(self.risk, cash_rials=9999)},
            {"risk": replace(self.risk, entries_allowed=False)},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.build(**params)

    def test_immutable_and_foreign_database_untouched(self):
        with closing(sqlite3.connect(self.db)) as db:
            for statement in ("UPDATE requests SET payload='different'", "DELETE FROM requests",
                              "UPDATE audit SET actor='operator'", "DELETE FROM audit",
                              "INSERT OR REPLACE INTO requests SELECT * FROM requests",
                              "INSERT OR REPLACE INTO audit SELECT * FROM audit"):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
        ledger = self.db.with_name("paper_trading.db")
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute("CREATE TABLE account (cash INT)")
            db.execute("INSERT INTO account VALUES (1000000)")
        ledger.chmod(0o600)
        before = ledger.read_bytes()
        with self.assertRaisesRegex(ValueError, "not a Phase 1"):
            ApprovalStore(ledger, dashboard.phase1_eligible).initialize()
        self.assertEqual(ledger.read_bytes(), before)

    def test_symlink_and_hardlink_cannot_be_used_as_approval_db(self):
        symbolic = self.db.with_name("symbolic.sqlite3")
        symbolic.symlink_to(self.db)
        with self.assertRaisesRegex(ValueError, "unsafe approval database"):
            ApprovalStore(symbolic, dashboard.phase1_eligible).initialize()
        # Android's Python may not provide link(2); simulate only the link-count check.
        lstat = Path.lstat

        def multiple_links(path):
            info = list(lstat(path))
            if path == self.db:
                info[3] = 2
            return stat_result(info)

        with patch.object(Path, "lstat", multiple_links), self.assertRaisesRegex(ValueError, "unsafe approval database"):
            ApprovalStore(self.db, dashboard.phase1_eligible).initialize()

    def test_database_rejects_late_terminal_insert(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid approval transition"):
                db.execute("INSERT INTO audit VALUES (?, ?, 'APPROVED', 'operator', 'test')",
                           (self.row()["id"], self.row()["valid_until"]))
        self.assertEqual(self.row()["state"], "PENDING")

class DashboardHTTPTests(ApprovalFixture):
    def setUp(self):
        super().setUp()
        self.server = dashboard.DashboardServer(0, self.store, [], clock=lambda: self.now)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.stop)
        row = self.row()
        self.url = "/review/" + row["id"]
        self.form = {"csrf": self.server.csrf, "fingerprint": row["fingerprint"],
                     "confirmed": "yes", "decision": "APPROVED"}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def http(self, method, url, *, data=None, headers=None):
        with closing(HTTPConnection(*self.server.server_address, timeout=3)) as connection:
            connection.request(method, url, body=data, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()

    def post(self, *, updates=None, headers=None, data=None):
        return self.http("POST", self.url,
                         data=urlencode(self.form | (updates or {})) if data is None else data,
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  "Origin": self.server.origin, **(headers or {})})[0]

    def test_review_headers_escaping_and_explicit_approval(self):
        status, headers, html = self.http("GET", self.url)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn('lang="fa" dir="rtl"', html)
        self.assertIn("fixture &lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertEqual(self.row()["state"], "PENDING")
        self.assertEqual(self.post(), 303)
        self.assertEqual(self.post(), 409)
        self.assertNotIn('<form', self.http("GET", self.url)[2])
        self.assertEqual(self.audit(), [("PENDING", "system"), ("APPROVED", "operator")])

    def test_rejects_csrf_wrong_origin_rebinding_and_ambiguous_form(self):
        self.assertEqual(self.http("GET", self.url, headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.http("POST", self.url, data=urlencode(self.form))[0], 403)
        self.assertEqual(self.post(headers={"Origin": "http://evil.example"}), 403)
        self.assertEqual(self.post(headers={"Host": "evil.example"}), 403)
        self.assertEqual(self.post(headers={"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(self.post(updates={"csrf": "a" * 64}), 403)
        self.assertEqual(self.post(updates={"confirmed": ""}), 400)
        self.assertEqual(self.post(updates={"decision": ""}), 400)
        self.assertEqual(self.post(data=urlencode(self.form) + "&decision=REJECTED"), 400)
        self.assertEqual(self.post(data="a" * 1025), 400)
        self.assertEqual(self.post(updates={"fingerprint": "0" * 64}), 409)
        self.assertEqual(self.http("GET", self.url + "?decision=APPROVED")[0], 404)
        self.assertEqual(self.audit(), [("PENDING", "system")])

    def test_expired_page_does_not_write_and_stale_form_cannot_approve(self):
        self.now = self.proposal.valid_until
        self.assertNotIn("<form", self.http("GET", self.url)[2])
        self.assertEqual(self.row()["state"], "PENDING")
        self.assertEqual(self.post(), 409)
        self.assertEqual(self.audit(), [("PENDING", "system"), ("EXPIRED", "system")])

    def test_changed_proposal_disappears_from_active_list_without_get_mutation(self):
        self.current = [self.build(rank={**self.rank, "delta": .65})]
        self.assertIn("پیشنهاد واجد شرایط وجود ندارد", self.http("GET", "/")[2])
        self.assertNotIn("<form", self.http("GET", self.url)[2])
        self.assertEqual(self.audit(), [("PENDING", "system")])
        self.assertEqual(self.post(), 409)
        self.assertEqual(self.audit(), [("PENDING", "system"), ("CANCELLED", "system")])

    def test_form_from_previous_server_cannot_be_replayed_after_restart(self):
        with dashboard.DashboardServer(0, self.open_store(), []) as restarted:
            self.assertNotEqual(self.form["csrf"], restarted.csrf)
            thread = Thread(target=restarted.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            try:
                with closing(HTTPConnection(*restarted.server_address, timeout=3)) as connection:
                    connection.request("POST", self.url, body=urlencode(self.form),
                                       headers={"Origin": restarted.origin,
                                                "Content-Type": "application/x-www-form-urlencoded"})
                    with connection.getresponse() as response:
                        self.assertEqual(response.status, 403)
            finally:
                restarted.shutdown()
                thread.join()
        self.assertEqual(self.row()["state"], "PENDING")


class ProductionBoundaryTests(unittest.TestCase):
    def test_ranked_archive_remains_observation_and_creates_no_proposal(self):
        day = "2026-01-01"
        observation = dict(capture_id="manual:7", capture_count=2, receipt_tehran=day + "T12:00:00+03:30",
                           receipt_utc=day + "T08:30:00+00:00", body_sha256="0" * 64, contract_count=2,
                           contracts={"1": {"two_sided": True, "candidate_eligible": True}},
                           rank_eligible_count=1, candidate_rejections={"no_bid_depth": 1},
                           rank_rejections={})
        with patch.object(dashboard.shadow_multiday, "build_report",
                          return_value={"days": {day: observation}}):
            cards = dashboard.archive_summary()
        self.assertEqual(cards[0]["candidate_count"], 1)
        with TemporaryDirectory() as scratch:
            store = ApprovalStore(Path(scratch) / "approvals.sqlite3", dashboard.phase1_eligible)
            store.initialize()
            store.reconcile()
            html = dashboard.home(store, cards, False, datetime(2026, 1, 2, tzinfo=timezone.utc)).decode()
            self.assertFalse(store.recent())
            self.assertIn("پیشنهاد واجد شرایط وجود ندارد", html)
            self.assertIn("بدون عمق خرید", html)
            self.assertIn("زمان مظنهٔ بورس نیست", html)
