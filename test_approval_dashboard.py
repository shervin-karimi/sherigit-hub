"""Offline tests: the only sockets opened by this module are loopback HTTP."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from os import stat_result
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
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

    def preflight_form(self):
        return {
            "csrf": self.server.csrf, "instrument": "ضـخود۱۱۶۵", "symbol": "", "kind": "CALL", "expiry": "2026-02-01",
            "strike": "650", "strike_unit": "rial", "price_unit": "toman_unit", "cash_unit": "rial",
            "contract_size": "۱۰۰۰", "limit_price": "۹", "quantity": "٢", "fee": "31",
            "initial_equity": "4000000", "committed": "10000", "cash": "200000",
            "observed_strike_unit": "", "strike_unit_source": "",
            "cash_settlement_date": "", "physical_settlement_date": "",
        }

    def preflight_post(self, updates=None, *, data=None, headers=None):
        return self.http("POST", "/preflight",
                         data=urlencode(self.preflight_form() | (updates or {})) if data is None else data,
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  "Origin": self.server.origin, **(headers or {})})

    def test_preflight_navigation_and_exact_rial_calculation(self):
        self.assertIn('href="/preflight"', self.http("GET", "/")[2])
        status, headers, html = self.http("GET", "/preflight")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
        self.assertIn('method="post" action="/preflight"', html)
        self.assertIn('autocomplete="off"', html)
        self.assertIn("اسکرین‌شات مظنهٔ تأییدشده نیست", html)
        self.assertEqual(self.http("GET", "/preflight?fee=0")[0], 404)
        status, headers, html = self.preflight_post(headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertIn("ضـخود1165", html)
        for label, amount in (
            ("قیمت اعمال", "۶۵۰ ریال به‌ازای هر واحد پایه"),
            ("قیمت محدود خرید (پریمیوم واردشده)", "۹ تومان به‌ازای هر واحد پایه"),
            ("ضریب (اندازهٔ قرارداد)", "۱٬۰۰۰ واحد پایه در هر قرارداد"),
            ("تعداد سفارش", "۲ قرارداد"),
            ("بهای هر قرارداد، پیش از کارمزد", "۹۰٬۰۰۰ ریال به‌ازای هر قرارداد"),
            ("بهای خرید به قیمت محدود، پیش از کارمزد", "۱۸۰٬۰۰۰ ریال"),
            ("کارمزد واردشده", "۳۱ ریال"),
            ("جمع برآورد خرید با کارمزد", "۱۸۰٬۰۳۱ ریال"),
            ("وجه نقد در دسترس واردشده", "۲۰۰٬۰۰۰ ریال"),
        ):
            with self.subTest(label=label):
                self.assertIn(f'<dt>{label}</dt><dd><bdi class="number" dir="ltr">{amount}</bdi></dd>', html)
        self.assertIn("غیرقابل اجرا", html)
        self.assertNotIn('<ul role="alert">', html)

    def test_preflight_per_contract_does_not_multiply_again_and_keeps_large_integer(self):
        status, _, html = self.preflight_post({
            "kind": "PUT", "price_unit": "rial_contract", "limit_price": "9007199254740993",
            "quantity": "2", "fee": "7", "initial_equity": "999999999999999999",
            "cash": "999999999999999999",
        })
        self.assertEqual(status, 200)
        self.assertIn("۱۸٬۰۱۴٬۳۹۸٬۵۰۹٬۴۸۱٬۹۹۳ ریال", html)
        self.assertIn("ریال به‌ازای هر قرارداد", html)
        self.assertNotIn('<ul role="alert">', html)

    def test_preflight_rejects_unknown_units_zero_quantity_and_invalid_amounts(self):
        for fields, notice in (
            ({"price_unit": ""}, "واحد و مبنای قیمت محدود خرید"),
            ({"strike_unit": "raw"}, "واحد قیمت اعمال: نامعتبر"),
            ({"contract_size": ""}, "اندازهٔ قرارداد"),
            ({"quantity": "۰"}, "تعداد قرارداد"),
            ({"quantity": ""}, "تعداد قرارداد"),
            ({"fee": ""}, "کارمزد کل خرید اعلام‌شده"),
            ({"fee": "-1"}, "کارمزد کل خرید اعلام‌شده"),
            ({"limit_price": "1e3"}, "قیمت محدود خرید"),
            ({"limit_price": "۱۲٫۵"}, "قیمت محدود خرید"),
            ({"limit_price": "9" * 19}, "قیمت محدود خرید"),
            ({"initial_equity": "0"}, "سرمایهٔ اولیه"),
            ({"expiry": "2026-02-30"}, "سررسید میلادی معتبر"),
        ):
            with self.subTest(fields=fields):
                status, _, html = self.preflight_post(fields)
                self.assertEqual(status, 400)
                self.assertIn("محاسبه نامعتبر است", html)
                self.assertIn(notice, html)
                self.assertNotIn("جمع برآورد خرید با کارمزد", html)

    def test_preflight_reports_missing_or_invalid_individual_required_fields(self):
        for name, label, invalid in (
            ("instrument", "شناسهٔ ابزار", "0"),
            ("kind", "نوع قرارداد", "BUY"),
            ("expiry", "سررسید", "2026-13-01"),
            ("strike", "قیمت اعمال", "0"),
            ("strike_unit", "واحد قیمت اعمال", "rial/toman"),
            ("price_unit", "واحد و مبنای قیمت محدود خرید", "toman"),
            ("cash_unit", "واحد کارمزد کل، سرمایهٔ اولیه، تعهدها و وجه نقد در دسترس", "toman"),
            ("contract_size", "اندازهٔ قرارداد", "0"),
            ("limit_price", "قیمت محدود خرید", "10 ریال"),
            ("quantity", "تعداد قرارداد", "-2"),
            ("fee", "کارمزد کل خرید اعلام‌شده", "31 تومان"),
            ("initial_equity", "سرمایهٔ اولیه", "-1"),
            ("committed", "بهای خرید باز و تعهد خریدهای دیگر", "1,000"),
            ("cash", "وجه نقد در دسترس", "-1"),
        ):
            for value in (None, "", invalid):
                with self.subTest(name=name, value=value):
                    form = self.preflight_form()
                    if value is None:
                        del form[name]
                    else:
                        form[name] = value
                    status, _, html = self.preflight_post(data=urlencode(form))
                    self.assertEqual(status, 400)
                    error, = re.findall(r'<ul role="alert"><li>([^<]+)</li></ul>', html)
                    self.assertIn(label, error)
                    self.assertNotIn("جمع برآورد خرید با کارمزد", html)

    def test_preflight_manual_checklist_is_visible_and_never_authorizes(self):
        for response in (self.http("GET", "/preflight"), self.preflight_post(), self.preflight_post({"cash": ""})):
            html = response[2]
            self.assertIn('lang="fa" dir="rtl"', html)
            checklist, = re.findall(r'<section class="preflight-checklist">(.*?)</section>', html, re.S)
            for text in ("ایزی‌تریدر رسمی", "شناسهٔ ابزار، نماد، CALL/PUT", "bid/ask", "وضعیت جلسه",
                         "کارمزد کل، وجه کل لازم", "سقف تجمیعی 10٪", "حداکثر زیان ممکن", "برنامهٔ خروج",
                         "سفارش‌های موجود، پرشدن‌های جزئی و لغوهای در انتظار", "تا روشن شدن وضعیت، دوباره ارسال نکنید",
                         "اگر موردی بررسی نشده یا مبهم است، سفارش ندهید", "تکمیل چک‌لیست مجوز معامله نیست"):
                self.assertIn(text, checklist)
            self.assertNotIn("<input", checklist)
            self.assertNotIn("<form", checklist)

    def test_preflight_expiry_warning_at_seven_tehran_days(self):
        self.now = datetime(2026, 1, 1, 21, tzinfo=timezone.utc)  # Jan 2 in Tehran.
        for expiry, notice in (
            ("2026-01-01", "تاریخ سررسید واردشده گذشته است"),
            ("2026-01-02", "سررسید نزدیک است"),
            ("2026-01-09", "سررسید نزدیک است"),
            ("2026-01-10", None),
        ):
            with self.subTest(expiry=expiry):
                status, _, html = self.preflight_post({"expiry": expiry})
                self.assertEqual(status, 200)
                if notice:
                    self.assertIn(notice, html)
                else:
                    self.assertNotIn('<ul role="alert">', html)

    def test_preflight_allocation_and_cash_boundaries(self):
        for committed, fee, cash, notice in (
            ("220000", "0", "180000", None),  # Equality is allowed by the numeric cap.
            ("220001", "0", "180000", "از سقف 10٪ سرمایهٔ اولیه بیشتر است"),
            ("220000", "1", "180001", "با احتساب کارمزد، برآورد از سقف 10٪ بیشتر است"),
            ("0", "31", "180030", "از وجه نقد در دسترس واردشده بیشتر است"),
        ):
            with self.subTest(committed=committed, fee=fee, cash=cash):
                status, _, html = self.preflight_post({"committed": committed, "fee": fee, "cash": cash})
                self.assertEqual(status, 200)
                if notice:
                    self.assertIn(notice, html)
                else:
                    self.assertNotIn('<ul role="alert">', html)
                self.assertIn("غیرقابل اجرا", html)
        status, _, html = self.preflight_post({"committed": "220001", "cash": "180030"})
        self.assertEqual(status, 200)
        alerts, = re.findall(r'<ul role="alert">(.*?)</ul>', html)
        self.assertIn("از سقف 10٪ سرمایهٔ اولیه بیشتر است", alerts)
        self.assertIn("از وجه نقد در دسترس واردشده بیشتر است", alerts)
        self.assertLess(html.index(alerts), html.index("جمع برآورد خرید با کارمزد"))

    def test_preflight_blocks_host_origin_csrf_xss_and_malformed_form(self):
        form = urlencode(self.preflight_form())
        self.assertEqual(self.http("GET", "/preflight", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.http("POST", "/preflight", data=form)[0], 403)
        for headers in ({"Host": "evil.example"}, {"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.preflight_post(headers=headers)[0], 403)
        self.assertEqual(self.preflight_post({"csrf": "0" * 64})[0], 403)
        for body in (form + "&quantity=5", form.replace("strike_unit=rial", "strike_unit=rial&strike_unit=toman"),
                     form + "&decision=APPROVED", form + "&total=1", form + "&x=%FF",
                     form.replace("strike=650", "strike=%ZY"), form.replace("strike=650", "strike=%FF"),
                     "x" * (dashboard.PREFLIGHT_MAX_FORM + 1)):
            with self.subTest(length=len(body)):
                self.assertEqual(self.preflight_post(data=body)[0], 400)
        for headers in ({"Content-Type": "multipart/form-data"}, {"Transfer-Encoding": "chunked"}):
            self.assertEqual(self.preflight_post(headers=headers)[0], 400)
        status, _, html = self.preflight_post({"instrument": '"><script>alert(1)</script>'})
        self.assertEqual(status, 400)
        self.assertNotIn("alert(1)", html)
        self.assertNotIn("<script", html)
        status, _, html = self.preflight_post({"cash_unit": '"><img src=x onerror=alert(1)>'})
        self.assertEqual(status, 400)
        self.assertNotIn("onerror", html)
        self.assertEqual(self.audit(), [("PENDING", "system")])

    def test_preflight_rejects_null_and_foreign_origins_even_with_valid_csrf(self):
        self.server.cache = Mock()
        self.server.cache.get.side_effect = AssertionError("unexpected archive lookup")
        with patch.object(dashboard, "manual_contract", side_effect=AssertionError("unexpected parsing")):
            for origin in ("null", "http://evil.example"):
                with self.subTest(origin=origin):
                    status, _, html = self.preflight_post(headers={
                        "Origin": origin, "Sec-Fetch-Site": "same-origin",
                    })
                    self.assertEqual(status, 403)
                    self.assertIn("مبدأ درخواست معتبر نیست", html)
            status, _, html = self.preflight_post({"csrf": "0" * 64}, headers={"Sec-Fetch-Site": "same-origin"})
            self.assertEqual(status, 403)
            self.assertIn("نشانهٔ امنیتی نامعتبر است", html)
        self.server.cache.get.assert_not_called()

    def test_preflight_rejects_duplicate_origin_headers(self):
        body = urlencode(self.preflight_form()).encode("ascii")
        for second in (self.server.origin, "null", "http://evil.example"):
            with self.subTest(second=second), closing(HTTPConnection(*self.server.server_address, timeout=3)) as connection:
                connection.putrequest("POST", "/preflight")
                connection.putheader("Content-Type", "application/x-www-form-urlencoded")
                connection.putheader("Content-Length", str(len(body)))
                connection.putheader("Sec-Fetch-Site", "same-origin")
                connection.putheader("Origin", self.server.origin)
                connection.putheader("Origin", second)
                connection.endheaders(body)
                with connection.getresponse() as response:
                    self.assertEqual(response.status, 403)
                    self.assertIn("مبدأ درخواست معتبر نیست", response.read().decode())

    def test_preflight_with_cached_match_never_accesses_approval_store_files_or_order_path(self):
        self.cache_contracts()
        before = self.db.read_bytes()
        original_files = set(self.db.parent.iterdir())
        with patch("sqlite3.connect", side_effect=AssertionError("unexpected database access")), \
                patch.object(Path, "open", side_effect=AssertionError("unexpected filesystem access")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("unexpected outbound request")), \
                patch.object(self.store, "decide", side_effect=AssertionError("unexpected decision")), \
                patch.object(dashboard.collector, "fetch", side_effect=AssertionError("unexpected market fetch")), \
                patch("paper_trading.PaperTrading.buy", side_effect=AssertionError("unexpected buy")), \
                patch("paper_trading.PaperTrading.sell", side_effect=AssertionError("unexpected sell")):
            self.assertEqual(self.http("GET", "/preflight")[0], 200)
            status, headers, html = self.preflight_post(
                self.broker_contract(observed_strike_unit="rial", strike_unit_source="broker_screen",
                                     cash_settlement_date="1405/11/06", physical_settlement_date="1405/11/07"),
                headers={"Sec-Fetch-Site": "same-origin"})
            self.assertEqual(self.preflight_post(headers={"Origin": "null"})[0], 403)
            blank = self.http("GET", "/preflight")[2]
            self.assertNotIn("9007199254740993", blank)
            self.assertNotIn("1405/11/06", blank)
        self.assertEqual(status, 200)
        self.assertEqual(self.comparison_statuses(html), ["MATCHED_OBSERVATIONAL"] * 6)
        self.assertEqual(self.evidence_statuses(html), ["MATCHED_OBSERVATIONAL", "UNKNOWN", "UNKNOWN"])
        self.assertIn("فقط پژوهش — حتی تطابق همهٔ ورودی‌ها مجوز معاملهٔ واقعی نیست.", html)
        self.assertNotIn("Location", headers)
        self.assertNotIn("/review/", html)
        for text in ("verified", "approved for trading", "execution ready", "trade-approved", "execution-ready"):
            self.assertNotIn(text, html.lower())
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(set(self.db.parent.iterdir()), original_files)
        self.assertEqual(self.audit(), [("PENDING", "system")])

    def broker_contract(self, **changes):
        return {"instrument": "۹۰۰۷۱۹۹۲۵۴۷۴۰۹۹۳", "symbol": "ضـخود1165"} | changes

    def archived_contract(self, **changes):
        return dict(symbol="ضـخود1165", option_type="CALL", maturity_date="20260201",
                    strike_price=650, strike_unit="rial", contract_size=1000) | changes

    def cache_contracts(self, contracts=None, *, failed=False, **changes):
        def observation(rows, day):
            return dict(contracts=rows, receipt_utc=f"{day}T07:00:00+00:00",
                        receipt_tehran=f"{day}T10:30:00+03:30")

        rows = {"9007199254740993": self.archived_contract()}
        report = dict(dataset_class=dashboard.watch.DATA_CLASS, source_url=dashboard.watch.SOURCE, days={
            "2026-01-01": observation(rows if contracts is None else contracts, "2026-01-01"),
            "2025-12-31": observation(rows, "2025-12-31"),
        }) | changes
        self.server.cache = SimpleNamespace(get=lambda: (report, failed))

    def comparison_statuses(self, html, *, evidence=False):
        rows = re.findall(r'<td><bdi dir="ltr">(MATCHED_OBSERVATIONAL|MISMATCH|UNKNOWN)</bdi></td>', html)
        return rows[6:] if evidence else rows[:6]

    def evidence_statuses(self, html):
        return self.comparison_statuses(html, evidence=True)

    def test_preflight_settlement_observations_do_not_use_archive_expiry_or_lookalike_fields(self):
        self.cache_contracts({"60210342490439455": self.archived_contract(
            maturity_date="20270127", strike_unit=None,
            cash_settlement_date="1405/11/05", physical_settlement_date="1405/11/07")})
        status, headers, html = self.preflight_post(self.broker_contract(
            instrument="60210342490439455", expiry="2027-01-27",
            observed_strike_unit="rial", strike_unit_source="operator_stated",
            cash_settlement_date="۱۴۰۵/۱۱/۰۶", physical_settlement_date="١٤٠٥/١١/٠٧"))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertEqual(self.comparison_statuses(html),
                         ["MATCHED_OBSERVATIONAL"] * 3 + ["UNKNOWN"] + ["MATCHED_OBSERVATIONAL"] * 2)
        self.assertEqual(self.evidence_statuses(html), ["UNKNOWN"] * 3)
        for text in ("1405/11/06", "1405/11/07", "واحد به گفتهٔ اپراتور", "در آرشیو نیست",
                     "شاهد رسمی مشخصات، اعمال و تسویه: در دسترس نیست (UNKNOWN)",
                     "کبیسه و برابری تقویمی احراز نشده‌اند", "۱۸۰٬۰۳۱ ریال"):
            self.assertIn(text, html)
        self.assertNotIn("1405/11/05", html)

    def test_preflight_flags_conflicting_manual_and_archive_units_without_comparing_amounts(self):
        for archive_unit, entered_unit, observed_unit, notice in (
            (None, "rial", "toman", "واحد مشاهده‌شدهٔ اعمال با واحد انتخاب‌شده در فرم تعارض دارد"),
            ("toman", "rial", "rial", "واحد تصریح‌شدهٔ اعمال در آرشیو با اظهار دستی متفاوت است"),
        ):
            with self.subTest(archive_unit=archive_unit):
                self.cache_contracts({"9007199254740993": self.archived_contract(strike_unit=archive_unit)})
                status, _, html = self.preflight_post(self.broker_contract(
                    strike_unit=entered_unit, observed_strike_unit=observed_unit, strike_unit_source="broker_screen"))
                self.assertEqual(status, 400)
                self.assertEqual(self.comparison_statuses(html)[3], "UNKNOWN")
                self.assertEqual(self.evidence_statuses(html), ["MISMATCH", "UNKNOWN", "UNKNOWN"])
                self.assertIn(notice, html)
                self.assertIn('class="identity-block"', html)
                self.assertNotIn("جمع برآورد خرید با کارمزد", html)

    def test_preflight_optional_evidence_never_fills_missing_unit_or_provenance(self):
        self.cache_contracts()
        status, _, html = self.preflight_post(self.broker_contract(
            strike_unit="", observed_strike_unit="rial", strike_unit_source="operator_stated"))
        self.assertEqual(status, 400)
        self.assertEqual(self.comparison_statuses(html)[3], "UNKNOWN")
        self.assertEqual(self.evidence_statuses(html), ["UNKNOWN"] * 3)
        self.assertIn("واحد قیمت اعمال: انتخاب نشده است", html)
        status, _, html = self.preflight_post(self.broker_contract(observed_strike_unit="rial"))
        self.assertEqual(status, 400)
        self.assertEqual(self.comparison_statuses(html)[3], "UNKNOWN")
        self.assertEqual(self.evidence_statuses(html), ["UNKNOWN"] * 3)
        self.assertIn("شاهد دستی واحد اعمال یا طبقه‌بندی منشأ آن ناقص است", html)
        status, _, html = self.preflight_post(self.broker_contract(strike_unit_source="broker_screen"))
        self.assertEqual(status, 400)
        self.assertEqual(self.evidence_statuses(html), ["UNKNOWN"] * 3)
        self.assertIn("واحد مشاهده‌شدهٔ قیمت اعمال: انتخاب نشده است", html)
        required_only = {k: v for k, v in self.preflight_form().items() if k not in dashboard.PREFLIGHT_EVIDENCE_FIELDS}
        self.assertEqual(self.preflight_post(data=urlencode(required_only))[0], 200)

    def test_preflight_rejects_malformed_jalali_dates_and_unsafe_evidence_without_echo(self):
        for fields in (
            {"cash_settlement_date": "1405-11-06"}, {"cash_settlement_date": "1405/13/01"},
            {"cash_settlement_date": "1405/07/31"}, {"cash_settlement_date": "0000/11/06"},
            {"physical_settlement_date": "1405/12/31"}, {"physical_settlement_date": "1405/11/00"},
            {"physical_settlement_date": "<script>alert(1)</script>"},
            {"strike_unit_source": "<script>alert(1)</script>"}, {"observed_strike_unit": "raw"},
        ):
            with self.subTest(fields=fields):
                status, _, html = self.preflight_post(fields)
                self.assertEqual(status, 400)
                self.assertIn("محاسبه نامعتبر است", html)
                self.assertNotIn("alert(1)", html)
        self.assertEqual(self.preflight_post(data=urlencode(self.preflight_form()) + "&cash_settlement_date=1405%2F11%2F06")[0], 400)

    def test_preflight_compares_all_fields_against_latest_receipt_by_exact_id(self):
        self.cache_contracts({
            "9007199254740993": self.archived_contract(strike_price=9007199254740993),
            "9007199254740992": self.archived_contract(option_type="PUT"),  # Ambiguous symbol, distinct IDs.
        })
        status, headers, html = self.preflight_post(self.broker_contract(strike="9007199254740993"))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("Manual EasyTrader vs archived contract comparison", html)
        self.assertEqual(self.comparison_statuses(html), ["MATCHED_OBSERVATIONAL"] * 6)
        self.assertNotIn('class="identity-block"', html)
        self.assertIn("زمان دریافت HTTP، زمان مظنهٔ بورس نیست", html)
        self.assertIn("۲۰۲۶/۰۱/۰۱", html)
        self.assertNotIn("۲۰۲۵/۱۲/۳۱", html)
        self.assertIn("۱۸۰٬۰۳۱ ریال", html)

    def test_preflight_reports_each_explicit_mismatch_and_blocks(self):
        self.cache_contracts()
        for index, update in enumerate((
                dict(instrument="9007199254740992"), dict(symbol="ضـخود1166"), dict(kind="PUT"),
                dict(strike="651"), dict(expiry="2026-02-02"), dict(contract_size="2"),
        )):
            with self.subTest(update=update):
                status, _, html = self.preflight_post(self.broker_contract(**update))
                results = ["UNKNOWN" if index == 0 else "MATCHED_OBSERVATIONAL"] * 6
                results[index] = "MISMATCH"
                self.assertEqual(status, 200)
                self.assertEqual(self.comparison_statuses(html), results)
                self.assertIn('class="identity-block"', html)

    def test_preflight_does_not_guess_strike_units_or_coerce_numeric_types(self):
        for unit, strike, broker_unit, expected_http in (
            (None, 650, "rial", 200), ("toman", 65, "rial", 400), ("rial", 650, "", 400),
            ("rial_contract", 650, "rial", 200), ("rial", 650.0, "rial", 200), ("rial", True, "rial", 200),
        ):
            with self.subTest(unit=unit, strike=strike, broker_unit=broker_unit):
                self.cache_contracts({"9007199254740993": self.archived_contract(strike_unit=unit, strike_price=strike)})
                status, _, html = self.preflight_post(self.broker_contract(strike_unit=broker_unit))
                self.assertEqual(status, expected_http)
                self.assertEqual(self.comparison_statuses(html),
                                 ["MATCHED_OBSERVATIONAL"] * 3 + ["UNKNOWN"] + ["MATCHED_OBSERVATIONAL"] * 2)
                if unit is None:
                    self.assertIn("650 · واحد نامعلوم", html)

    def test_preflight_symbol_only_requires_id_and_rejects_ambiguity(self):
        self.cache_contracts()
        status, _, html = self.preflight_post(self.broker_contract(instrument=""))
        self.assertEqual(status, 200)
        self.assertEqual(self.comparison_statuses(html), ["UNKNOWN"] + ["MATCHED_OBSERVATIONAL"] * 5)
        self.assertIn('class="identity-block"', html)
        self.cache_contracts({"1": self.archived_contract(), "2": self.archived_contract(symbol="ضـخود۱۱۶۵")})
        for supplied_id in ("", "3"):
            with self.subTest(supplied_id=supplied_id):
                _, _, html = self.preflight_post(self.broker_contract(instrument=supplied_id))
                self.assertEqual(self.comparison_statuses(html), ["UNKNOWN"] * 6)
                self.assertIn("هیچ قراردادی انتخاب نشد", html)

    def test_preflight_missing_identity_archive_instrument_and_future_receipt(self):
        self.cache_contracts()
        status, _, html = self.preflight_post(self.broker_contract(instrument="", symbol=""))
        self.assertEqual(status, 400)
        self.assertEqual(self.comparison_statuses(html), ["UNKNOWN"] * 6)
        self.assertIn('class="identity-block"', html)
        for settings, message in (
            ({"days": {}}, "هیچ مشاهدهٔ آرشیوشده‌ای"),
            ({"contracts": {}}, "قرارداد در آخرین ثبت یافت نشد"),
            ({"failed": True}, "اعتبارسنجی آرشیو ناموفق بود"),
            ({"dataset_class": "synthetic"}, "اعتبارسنجی آرشیو ناموفق بود"),
        ):
            with self.subTest(settings=settings):
                self.cache_contracts(**settings)
                status, _, html = self.preflight_post(self.broker_contract())
                self.assertEqual(status, 200)
                self.assertEqual(self.comparison_statuses(html), ["UNKNOWN"] * 6)
                self.assertIn(message, html)
                self.assertIn('class="identity-block"', html)
                self.assertIn("۱۸۰٬۰۳۱ ریال", html)
        self.cache_contracts()
        self.now -= timedelta(hours=6)
        _, _, html = self.preflight_post(self.broker_contract())
        self.assertEqual(self.comparison_statuses(html), ["UNKNOWN"] * 6)
        self.assertIn("از ساعت دستگاه جلوتر است", html)

    def test_preflight_uses_real_cache_and_fails_closed_after_archive_corruption(self):
        stream = self.db.parent / "isolated-test-stream"
        stream.mkdir()
        collector = dashboard.collector
        row = dict(uaInsCode="101", insCode_C="9007199254740993", lVal18AFC_C="ضـخود1165",
                   strikePrice=650, contractSize=1000, endDate="20260201", remainedDay=31,
                   pClosing_UA=1000, pDrCotVal_UA=1000)
        raw = collector.json_bytes({"instrumentOptMarketWatch": [row]})
        at = self.now - timedelta(minutes=1)
        with collector.locked(stream, exclusive=True) as fd:
            collector.append(stream, fd, [], raw, at - timedelta(seconds=1), at, {})
        self.server.cache = dashboard.research.ArchiveCache(stream, self.db.parent / "no-legacy")
        body = stream / "00000001/body.raw.json"
        before = {path: path.read_bytes() for path in body.parent.iterdir()}
        _, _, html = self.preflight_post(self.broker_contract())
        self.assertEqual(self.comparison_statuses(html),
                         ["MATCHED_OBSERVATIONAL"] * 3 + ["UNKNOWN"] + ["MATCHED_OBSERVATIONAL"] * 2)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        body.chmod(0o600)
        body.write_bytes(raw.replace(b"650", b"651"))
        _, _, html = self.preflight_post(self.broker_contract())
        self.assertIn("اعتبارسنجی آرشیو ناموفق بود", html)
        self.assertEqual(self.comparison_statuses(html), ["UNKNOWN"] * 6)

    def test_preflight_comparison_escapes_archive_symbol_and_rejects_malformed_manual_symbol(self):
        self.cache_contracts({"9007199254740993": self.archived_contract(symbol='"><script>archive</script>')})
        status, _, html = self.preflight_post(self.broker_contract())
        self.assertEqual(status, 200)
        self.assertIn("&quot;&gt;&lt;script&gt;archive&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        for symbol in ('"><script>broker</script>', "a" * 41, "abc\u202e"):
            with self.subTest(symbol=symbol):
                status, _, html = self.preflight_post(self.broker_contract(symbol=symbol))
                self.assertEqual(status, 400)
                self.assertIn("توقف بررسی هویت", html)
                self.assertNotIn(symbol, html)

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
