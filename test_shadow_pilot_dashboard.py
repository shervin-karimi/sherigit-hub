"""Offline integration tests for the read-only loopback pilot dashboard."""

from datetime import timedelta
import hashlib
from http.client import HTTPConnection
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import shadow_pilot as pilot
from shadow_pilot_dashboard import HOST, PilotServer
import test_shadow_pilot as synthetic


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = synthetic.PilotTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.output

    def ingest(self, *, paper=False):
        if paper:
            self.fixture.enable_paper()
            self.fixture.sign(1, entries=[("dashboard-test", 1)])
        return self.fixture.report()

    def start(self):
        # Keep non-loopback blocked by the suite audit hook. The fixture mocked
        # socket creation only for ingestion, so restore real sockets here.
        self.fixture.socket_guard.stop()
        self.server = PilotServer(self.root, 0, clock=lambda: self.fixture.now)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/", *, method="GET", headers=None):
        client = HTTPConnection(HOST, self.server.server_port, timeout=3)
        try:
            client.request(method, path, headers=headers or {})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            client.close()

    def digest(self):
        return {path.relative_to(self.root): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_daily_report_shows_both_sides_ownership_capacity_and_unverified_time(self):
        report = self.ingest(paper=True)
        self.assertEqual(set(report["مشاهدات و غربال پژوهشی"]["قراردادها"]), {"202", "303"})
        self.start()
        before = self.digest()
        ledger = self.fixture.paths["db"].read_bytes()
        with patch("paper_trading.PaperTrading.transaction", side_effect=AssertionError("ledger access")), \
                patch("shadow_collector.collect", side_effect=AssertionError("HTTP collection")):
            code, headers, html = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        for text in ('lang="fa" dir="rtl"', "CALL", "PUT", "دوطرفه", "تعداد معاملات ناکافی",
                     "دریافت HTTP تهران", "زمان معتبر مظنه در بورس", "تأیید نشده",
                     "sample CALL", "ظرفیت باقیماندهٔ خرید امروز", "ظرفیت فروش هر سفارش",
                     "ظرفیت باقی‌ماندهٔ مواجهه", "آخرین اجرای کامل UTC"):
            self.assertIn(text, html)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.fixture.paths["db"].read_bytes(), ledger)
        self.assertEqual(self.request(method="HEAD")[2], "")

    def test_pagination_filters_escaping_and_atomic_updates(self):
        report = self.ingest()
        original = report["مشاهدات و غربال پژوهشی"]["قراردادها"]["202"]
        rows = {str(1000 + i): dict(original, symbol=f"نماد {i:02}", candidate_eligible=i % 2 == 0,
                                   option_type="CALL" if i % 2 == 0 else "PUT") for i in range(42)}
        rows["1041"]["symbol"] = '<script>alert(1)</script>'
        report["مشاهدات و غربال پژوهشی"]["قراردادها"] = rows
        path = self.root / "reports" / "2026-11-22.json"
        pilot.atomic_json(path, report)
        self.start()
        self.assertIn("صفحهٔ بعد", self.request()[2])
        self.assertIn("صفحهٔ قبل", self.request("/?page=2")[2])
        found = self.request("/?" + urlencode({"kind": "PUT", "status": "rejected", "q": "1041"}))[2]
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", found)
        self.assertNotIn("<script>", found)
        self.assertNotIn(">1040<", found)
        report["دفتر آزمایشی"]["مانع"] = "به‌روزرسانی گزارش"
        pilot.atomic_json(path, report)
        self.assertIn("به‌روزرسانی گزارش", self.request()[2])

    def test_corrupt_report_never_promotes_archive_or_leaks_paths(self):
        report = self.ingest()
        self.start()
        path = self.root / "reports" / "2026-11-22.json"
        report["مشاهدات و غربال پژوهشی"]["آخرین ثبت"]["exchange_quote_timestamp"] = self.fixture.now.isoformat()
        pilot.atomic_json(path, report)
        status, _, html = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn(str(self.root), html)
        report["مشاهدات و غربال پژوهشی"]["آخرین ثبت"]["exchange_quote_timestamp"] = None
        report["دفتر آزمایشی"] = []
        pilot.atomic_json(path, report)
        self.assertEqual(self.request()[0], 503)
        path.unlink()
        private = self.fixture.root / "private.json"
        private.write_text('{"secret":"do not disclose"}', encoding="utf-8")
        path.symlink_to(private)
        self.assertNotIn("do not disclose", self.request()[2])
        self.assertEqual(self.request()[0], 503)

    def test_host_origin_methods_and_date_limits(self):
        self.ingest()
        self.start()
        self.assertEqual(self.request(headers={"Host": "example.org"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "https://example.org"})[0], 403)
        self.assertEqual(self.request(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT"):
            code, headers, _ = self.request(method=method)
            self.assertEqual((code, headers["Allow"]), (405, "GET, HEAD"))
        for path in ("/?day=../../etc", "/?day=2026-11-23", "/?kind=CALL&kind=PUT", "/?q=%Z1"):
            self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/reports/state.json")[0], 404)

    def test_expired_capacity_and_unhealthy_scheduler_are_visible(self):
        self.ingest(paper=True)
        self.start()
        self.fixture.now += timedelta(minutes=6)
        html = self.request()[2]
        self.assertIn("گزارش قدیمی یا ناقص", html)
        self.assertIn("ظرفیت خرید در زمان گزارش ثبت شده است", html)
        self.assertIn("<td>نامعلوم</td>", html)
        self.fixture.now += timedelta(days=1)
        self.assertEqual(self.request()[0], 503)
        self.assertIn("این گزارش تاریخی یا قدیمی است", self.request("/?day=2026-11-22")[2])

    def test_recent_incomplete_tick_hides_capacity_despite_previous_heartbeat(self):
        self.ingest(paper=True)
        self.start()
        no_capacity = "<td>نامعلوم</td>"
        self.assertNotIn(no_capacity, self.request()[2])
        state_path = self.root / "state.json"
        state = pilot.load_state(state_path)
        self.fixture.now += timedelta(seconds=30)
        state["last_tick"] = self.fixture.now.isoformat()
        pilot.atomic_json(state_path, state)
        before = self.digest()
        code, _, html = self.request()
        self.assertEqual(code, 200)
        self.assertIn("گزارش قدیمی یا ناقص", html)
        self.assertIn(no_capacity, html)
        self.assertEqual(self.digest(), before)

    def test_sell_capacity_expires_even_while_report_is_fresh(self):
        self.fixture.enable_paper()
        self.fixture.sign(1, entries=[("dashboard-expiry", 1)], quote_changes={
            "quoted_at": (self.fixture.now - timedelta(minutes=4)).isoformat(),
        })
        report = self.fixture.report()
        rows = report["دفتر آزمایشی"]["موقعیت‌های ربات"]
        self.assertEqual(rows[0]["ظرفیت فروش هر سفارش"], 1)
        self.start()
        self.fixture.now += timedelta(seconds=59)
        self.assertNotIn("<td>نامعلوم</td>", self.request()[2])
        self.fixture.now += timedelta(seconds=1)
        code, _, html = self.request()
        self.assertEqual(code, 200)
        self.assertNotIn("این گزارش تاریخی یا قدیمی است", html)
        self.assertIn("<td>نامعلوم</td>", html)

    def test_report_without_contract_table_fails_closed(self):
        report = self.ingest()
        del report["مشاهدات و غربال پژوهشی"]["قراردادها"]
        pilot.atomic_json(self.root / "reports" / "2026-11-22.json", report)
        self.start()
        before = self.digest()
        code, _, html = self.request()
        self.assertEqual(code, 503)
        self.assertIn("گزارش این تاریخ در دسترس نیست", html)
        self.assertNotIn(str(self.root), html)
        self.assertEqual(self.digest(), before)


if __name__ == "__main__":
    unittest.main()
