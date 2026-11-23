"""Phase 2 offline tests; fixtures are isolated from the market archive and ledger."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from http.client import HTTPConnection
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import approval_dashboard as dashboard
from approval_workflow import ApprovalStore
import dashboard_research as research
import shadow_collector as collector
import shadow_multiday
import shadow_watch as watch


DAY = "2026-11-22"
PREVIOUS = "2026-11-21"


def contract(**updates):
    return dict(symbol="اختیار", option_type="CALL", underlying_symbol="تاصيكو",
                underlying_code="101", strike_price=1000, maturity_date="20270120",
                contract_size=1000, best_bid=100, best_ask=102, bid_volume=10, ask_volume=9,
                trade_count=50, trade_volume=200, remaining_days=55, underlying_last_price=1000,
                underlying_closing_price=1000, spread_percent=1.980198, two_sided=True,
                candidate_eligible=True, candidate_rejection=None, rank_eligible=False,
                rank_rejection="فرض‌های Greeks هنوز تأیید نشده‌اند", iv_status="ok",
                greeks_assumption_status="experimental_unverified") | updates


def observation(day, contracts):
    return dict(contracts=contracts, body_sha256=day, capture_id="fixture", capture_count=1,
                receipt_tehran=day + "T10:30:00+03:30", receipt_utc=day + "T07:00:00+00:00",
                contract_count=len(contracts), rank_eligible_count=0,
                candidate_rejections={}, rank_rejections={})


class CandidateQueryTests(unittest.TestCase):
    def test_search_type_underlying_spread_and_screening_status(self):
        rows = {
            "111": contract(), "222": contract(option_type="PUT", spread_percent=1.5),
            "333": contract(option_type="PUT", spread_percent=2.5),
            "444": contract(option_type="PUT", underlying_symbol="پایه"),
            "555": contract(option_type="PUT", spread_percent=None, candidate_eligible=False),
        }

        def picked(**query):
            parsed = research.Query.parse(urlencode(query))
            return [code for code, _ in research.select_rows(rows, parsed)[0]]

        self.assertEqual(picked(kind="PUT", underlying="تاصیکو", spread="2"), ["222"])
        self.assertEqual(picked(q="تاصیکو", status="rejected"), ["555"])
        self.assertEqual(picked(q="33", status="all"), ["333"])
        self.assertEqual(picked(q="101"), ["222", "111", "444", "333"])
        self.assertEqual(picked(q="اختیار"), ["222", "111", "444", "333"])
        self.assertEqual(picked(status="rejected", spread="200"), [])

    def test_pagination_stability_and_missing_sort_values(self):
        contracts = {str(code): contract() for code in reversed(range(100, 143))}
        contracts["100"]["spread_percent"] = float("nan")
        contracts["101"]["spread_percent"] = 2.1
        first, total, page, pages = research.select_rows(contracts, research.Query(order="desc"))
        last, _, last_page, _ = research.select_rows(contracts, research.Query(page=99999, order="desc"))
        self.assertEqual((len(first), total, page, pages), (20, 43, 1, 3))
        self.assertEqual([code for code, _ in first[:3]], ["101", "102", "103"])
        self.assertEqual(([code for code, _ in last], last_page), (["141", "142", "100"], 3))

    def test_invalid_query_parameters(self):
        for raw in ("kind=call", "sort=best_bid", "order=zzz", "status=verified", "q=%",
                    "q=%FF", "q=%00", "q=%0A", "q=%E2%80%AE", "q=a&q=b", "q", "q=" + "a" * 81,
                    "page=0", "page=-1", "page=999999", "page=", "size=5000", "spread=201",
                    "spread=NaN", "spread=-1", "spread=1e2", "spread=1.0000001", "q=" + "a" * 2048):
            with self.subTest(raw=raw[:100]), self.assertRaises(ValueError):
                research.Query.parse(raw)


class DailyComparisonTests(unittest.TestCase):
    def test_comparison_joins_on_id_and_verified_matching_terms(self):
        older = observation(PREVIOUS, {"123": contract(), "124": contract(), "125": contract()})
        latest = observation(DAY, {
            "123": contract(symbol="نماد تغییر کرده", best_bid=101, bid_volume=11, spread_percent=.985222),
            "124": contract(contract_size=500),
            "126": contract(),
        })
        day, _, records, counts = research.daily_comparison(DAY, latest, {PREVIOUS: older})
        self.assertEqual(day, PREVIOUS)
        status, changes = records["123"]
        self.assertEqual((status, changes["best_bid"], changes["best_ask"], changes["spread_percent"],
                          changes["bid_volume"], changes["trade_count"]), ("observed", 1, 0, -.994976, 1, 0))
        self.assertEqual(records["124"], ("identity", {}))
        self.assertEqual(records["126"], ("absent", {}))
        self.assertEqual(counts["disappeared"], 1)
        self.assertEqual(counts["liquidity_changed"], 1)
        for field, value in (("option_type", "PUT"), ("underlying_code", "102"), ("strike_price", 1001),
                             ("maturity_date", "20270121"), ("contract_size", 1000.0)):
            self.assertEqual(research.compare_contract("123", contract(), contract(**{field: value})),
                             ("identity", {}))

    def test_no_comparison_if_missing_yesterday_same_body_or_unsuitable_quote(self):
        latest = observation(DAY, {"123": contract()})
        for days in ({}, {"2026-11-20": observation("2026-11-20", {})},
                     {PREVIOUS: dict(latest)}):
            self.assertEqual(research.daily_comparison(DAY, latest, days), (PREVIOUS, None, {}, {}))
        for overrides in ({"best_bid": 103}, {"best_ask": None}, {"bid_volume": 0},
                          {"spread_percent": float("inf")}, {"two_sided": False}):
            self.assertEqual(research.compare_contract("123", contract(), contract(**overrides)), ("quote", {}))
        status, changes = research.compare_contract("123", contract(), contract(trade_count=None, trade_volume=-1))
        self.assertEqual(status, "observed")
        self.assertNotIn("trade_count", changes)
        self.assertNotIn("trade_volume", changes)


class ArchiveCacheTests(unittest.TestCase):
    def test_head_integrity_and_repeated_requests_do_not_replay(self):
        with TemporaryDirectory() as scratch:
            stream = Path(scratch) / collector.SYNTHETIC
            stream.mkdir()
            cache = research.ArchiveCache(stream, Path(scratch) / "no-legacy")
            original = shadow_multiday.build_report

            def append(day, **fields):
                row = dict(uaInsCode="101", insCode_C="123", lVal18AFC_C="synthetic",
                           lval30_UA="synthetic underlying", strikePrice=1000, contractSize=1000,
                           endDate="20270120", remainedDay=55, pClosing_UA=1000, pDrCotVal_UA=1000,
                           pMeDem_C=100, pMeOf_C=102, qTitMeDem_C=10, qTitMeOf_C=9,
                           zTotTran_C=50, qTotTran5J_C=200) | fields
                at = datetime.fromisoformat(day + "T07:00:00+00:00")
                with collector.locked(stream, exclusive=True) as fd:
                    prior = collector.read_stream(stream, collector.SYNTHETIC)
                    return collector.append(stream, fd, prior,
                                            collector.json_bytes({"instrumentOptMarketWatch": [row]}),
                                            at - timedelta(seconds=1), at, {}, dataset_class=collector.SYNTHETIC)

            append(PREVIOUS)
            self.assertEqual(cache.get(), (None, True))  # production default rejects synthetic archives
            cache = research.ArchiveCache(stream, Path(scratch) / "no-legacy")
            with patch.object(shadow_multiday, "build_report", side_effect=lambda *args, **kw:
                              original(*args, **kw, dataset_class=collector.SYNTHETIC)) as replay:
                old, failed = cache.get()
                self.assertFalse(failed)
                self.assertIs(cache.get()[0], old)
                self.assertEqual(replay.call_count, 1)
                head = append(DAY, pMeDem_C=101)
                new, failed = cache.get()
                self.assertFalse(failed)
                self.assertEqual(new["manual_head_sha256"], head["head_sha256"])
                self.assertEqual(new["dashboard_comparison"][2]["123"][0], "observed")
                self.assertEqual(new["days"][DAY]["contracts"]["123"]["underlying_symbol"], "synthetic underlying")
                self.assertEqual(replay.call_count, 2)
                body = stream / "00000001/body.raw.json"
                raw = body.read_bytes()
                info = body.stat()
                body.chmod(0o600)
                body.write_bytes(raw.replace(b'"synthetic"', b'"tampering"', 1))
                body.chmod(stat.S_IMODE(info.st_mode))
                os.utime(body, ns=(info.st_atime_ns, info.st_mtime_ns))
                self.assertEqual(cache.get(), (None, True))
                self.assertEqual(cache.get(), (None, True))
                self.assertEqual(replay.call_count, 3)
                body.chmod(0o600)
                body.write_bytes(raw)
                self.assertFalse(cache.get()[1])
                self.assertEqual(replay.call_count, 4)


class CardHTML(HTMLParser):
    """Separate visible card text from text inside native, collapsed disclosures."""

    def __init__(self, html):
        super().__init__()
        self.cards = []
        self.card = None
        self.details = self.summary = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "article" and ("class", "candidate-card") in attrs:
            self.card = {"visible": [], "technical": [], "details": []}
            self.cards.append(self.card)
        if self.card is not None and tag == "details":
            self.details = True
            self.card["details"].append(dict(attrs))
        if tag == "summary":
            self.summary = True

    def handle_endtag(self, tag):
        if tag == "article":
            self.card = None
        if tag == "details":
            self.details = False
        if tag == "summary":
            self.summary = False

    def handle_data(self, text):
        if self.card is not None:
            self.card["technical" if self.details and not self.summary else "visible"].append(text)


class ResponsiveCardTests(unittest.TestCase):
    def test_card_grid_defaults_to_one_column_and_only_wide_landscape_opts_into_two(self):
        html = dashboard.page("آزمون", "").decode()
        self.assertIn('lang="fa" dir="rtl"', html)
        self.assertIn('content="width=device-width, initial-scale=1"', html)
        self.assertIn(".candidate-grid{display:grid;grid-template-columns:minmax(0,1fr)", html)
        wide_rule = ".candidate-grid{grid-template-columns:repeat(2,minmax(0,1fr))}"
        self.assertEqual(html.count(wide_rule), 1)
        self.assertRegex(html, r"@media \(min-width:64rem\) and \(orientation:landscape\)\{\s*"
                         + re.escape(wide_rule))
        self.assertRegex(html, r"\.technical-value\{[^}]*overflow-wrap:anywhere")
        self.assertNotIn("@import", html)

    def test_summary_keeps_technical_values_in_closed_section_and_status_visible(self):
        row = contract(symbol="نماد دیدنی", underlying_symbol="پایه دیدنی", best_bid=12345)
        latest = observation(DAY, {})
        latest.update(body_sha256="b" * 64, capture_id="capture-123")
        card, = CardHTML(dashboard.candidate_card("987654321", row, latest, None)).cards
        visible, technical = (" ".join(card[key]) for key in ("visible", "technical"))
        self.assertEqual(card["details"], [{"class": "candidate-details"}])
        for value in ("نماد دیدنی", "CALL", "پایه دیدنی", "۱۲٬۳۴۵", "۱۰۲", "۱٫۹۸۰۱۹۸٪", "۵۰",
                      "نامزد بررسی اولیه", "غیرقابل اجرا", "آزمایشی، تأیید نشده",
                      "محاسبه شده، تأیید نشده", "اولین رد رتبه‌بندی"):
            self.assertIn(value, visible)
        for value in ("987654321", "101", "capture-123", "b" * 64, watch.SOURCE, latest["receipt_tehran"],
                      latest["receipt_utc"], "20270120", "۲۰۲۷/۰۱/۲۰", "اندازهٔ قرارداد",
                      "experimental_unverified"):
            self.assertIn(value, technical)
            self.assertNotIn(value, visible)
        self.assertNotIn("ریال", visible + technical)

        rejected = contract(option_type="PUT", underlying_symbol=None, candidate_eligible=False,
                            candidate_rejection="no_bid_depth", rank_rejection=None,
                            greeks_assumption_status=None, iv_status=None)
        card, = CardHTML(dashboard.candidate_card("987654321", rejected, latest, None)).cards
        visible = " ".join(card["visible"])
        for value in ("PUT", "رد در غربال", "بدون عمق خرید", "Greeks : ارزیابی نشده",
                      "IV : ارزیابی نشده", "غیرقابل اجرا"):
            self.assertIn(value, visible)
        self.assertNotIn("101", visible)

    def test_observational_changes_keep_sign_in_ltr_and_receipt_keeps_tehran_day(self):
        comparison = (PREVIOUS, "2026-11-21T00:15:00+03:30",
                      {"123": ("observed", {"best_bid": 1250, "best_ask": -90,
                                            "spread_percent": -.25, "trade_count": 0})}, {})
        html = dashboard.comparison_details(comparison, "123")
        for value in ("+۱٬۲۵۰", "−۹۰", "−۰٫۲۵", "۰"):
            self.assertIn(f'<bdi class="number" dir="ltr">{value}</bdi>', html)
        self.assertIn("ناموجود", html)
        self.assertNotIn("+۰</bdi>", html)
        self.assertEqual(dashboard.display_receipt(comparison[1]),
                         '<bdi dir="ltr">۲۰۲۶/۱۱/۲۱</bdi>، ساعت <bdi dir="ltr">۰۰:۱۵:۰۰</bdi>')


class ResearchHTTPTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.now = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
        self.store = ApprovalStore(Path(scratch.name) / "approvals.sqlite3", dashboard.phase1_eligible,
                                   clock=lambda: self.now)
        self.store.initialize()
        self.store.reconcile()
        rows = {str(i): contract(symbol='"><script>اختبار', underlying_symbol="<base>",
                                 rank_rejection="<rank>", iv_failure_reason="<IV>")
                for i in range(100, 143)}
        rows["222"] = contract(symbol="<rejected>", candidate_eligible=False, candidate_rejection="no_bid_depth",
                               rank_rejection=None, iv_status=None, greeks_assumption_status=None,
                               spread_percent=None, two_sided=False)
        latest = observation(DAY, rows)
        days = {DAY: latest, PREVIOUS: observation(PREVIOUS, {"100": contract()})}
        report = dict(days=days, dashboard_comparison=research.daily_comparison(DAY, latest, days))
        cache = Mock(get=lambda: (report, False))
        self.server = dashboard.DashboardServer(0, self.store, [], cache=cache, clock=lambda: self.now)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def get(self, path, headers=None):
        with closing(HTTPConnection(*self.server.server_address, timeout=3)) as connection:
            connection.request("GET", path, headers=headers or {})
            with connection.getresponse() as response:
                return response.status, dict(response.getheaders()), response.read().decode()

    def test_mobile_html_security_and_zero_eligible_proposals(self):
        status, headers, html = self.get("/?" + urlencode({"q": "<script>"}))
        self.assertEqual(status, 200)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        for snippet in ('lang="fa" dir="rtl"', 'name="viewport"', 'class="candidate-grid"',
                        'method="get"', 'href="#daily-comparison"', 'id="daily-comparison"', "&lt;script&gt;", "&lt;base&gt;",
                        "&lt;rank&gt;", "&lt;IV&gt;", "experimental_unverified", "نامزد بررسی اولیه",
                        "VERIFIED TRADE PROPOSAL", "پیشنهاد واجد شرایط وجود ندارد", "۴۳</bdi> نتیجه",
                        "صرفاً مقایسهٔ مشاهداتی", "فرادادهٔ فنی ثبت"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, html)
        for snippet in ('<script>', '<base>', '<rank>', 'method="post"', 'href="https:', 'src="https:',
                        "PRELIMINARY CANDIDATE"):
            self.assertNotIn(snippet, html)
        self.assertEqual(len(CardHTML(html).cards), 20)
        self.assertIn("q=%3Cscript%3E&amp;kind=", html)
        self.assertFalse(self.store.recent())

    def test_query_http_validation_paging_rejected_and_unavailable_comparison(self):
        self.assertEqual(self.get("/?page=1&status=all&status=passed")[0], 400)
        self.assertEqual(self.get("/?sort=spread", {"Host": "other.example"})[0], 403)
        status, _, last = self.get("/?page=3")
        self.assertEqual(status, 200)
        self.assertEqual(len(CardHTML(last).cards), 3)
        self.assertIn("شناسه در ثبت دیروز یافت نشد", last)
        rejected = self.get("/?status=rejected")[2]
        self.assertIn("&lt;rejected&gt;", rejected)
        self.assertIn("بدون عمق خرید", rejected)
        self.assertIn("ارزیابی نشده", rejected)
        self.now -= timedelta(hours=8)
        future = self.get("/")[2]
        self.assertIn("ثبت مناسب برای مقایسه در دسترس نیست", future)
        self.assertNotIn("اختلاف شمارنده‌های گزارش‌شده", future)


def full_market_fixture():
    options, bases = [], {}
    for i in range(23):
        base = str(81000 + i)
        tech = dict(status="NOT_AVAILABLE", reason_fa="تاریخچهٔ منطبق نیست", reason_code="history_id_not_found",
                    history=None)
        symbols = [f"پایه نمونه {i}"]
        if i == 0:
            symbols.append("<base-alias> نام تازه")
            tech.update(status="AVAILABLE", reason_fa="صرفاً توصیفی", reason_code="retrospective_history",
                        downloaded_after_capture=True, history={
                            "valid_close_count": 5, "session_date_cutoff_exclusive": DAY,
                            "adjustment_status": "UNKNOWN", "session_completeness": "UNKNOWN",
                            "indicators": {"SMA(5)": {"status": "COMPUTED", "value": 123},
                                           "MACD": {"status": "INSUFFICIENT_HISTORY", "reason": "<محدودیت>"}},
                        })
        group = []
        for j, kind in enumerate(("CALL", "PUT")):
            ins_code = str(90000 + 2 * i + j)
            screen = dict(candidate_eligible=j == 0, candidate_rejection=None if j == 0 else "wide_spread",
                          rank_eligible=False, rank_rejection="<rank>" if j == 0 else None,
                          iv_status="ok" if j == 0 else None,
                          greeks_assumption_status="experimental_unverified" if j == 0 else None)
            opt = contract(ins_code=ins_code, underlying_code=base, underlying_symbol=symbols[0], option_type=kind,
                           symbol="<script>alert(1)</script>" if i == j == 0 else f"اختیار نمونه {ins_code}",
                           status="REJECTED" if j else "UNKNOWN" if i else "RESEARCH_ONLY",
                           reason_code="wide_spread" if j else "history_id_not_found" if i else "descriptive_comparison",
                           reason_fa="شرح پژوهشی <نشانه>", screening=screen, quote_class="two_sided",
                           liquidity_issues=[] if j == 0 else ["trade_count_below_minimum_or_unusable"],
                           price_unit="UNKNOWN", depth_unit="UNKNOWN", multiplier_unit="UNKNOWN",
                           execution_eligible=False)
            if i == 22 and j == 0:
                opt.update(screening=None, quote_class="missing", best_bid=None, best_ask=None,
                           spread_percent=None, bid_volume=None, ask_volume=None, trade_count=0,
                           maturity_date="20270121")
            group.append(opt)
        bases[base] = dict(symbols=symbols, technical_analysis=tech) | research.combined.market_counts(group, True)
        options.extend(group)
    diagnostics = [dict(raw_row=24, side="C", ins_code=None, underlying_code="81000", status="UNKNOWN",
                        reason_code="missing_option_id", reason_fa="<رکورد ناقص>",
                        excluded_from_options=True)]
    capture = observation(DAY, {}) | dict(capture_id=research.COMBINED_CAPTURE, dataset_class=watch.DATA_CLASS,
                                          source_url=watch.SOURCE, screening_status="existing_replay", seal_sha256="a" * 64)
    counts = research.combined.market_counts(options, True)
    return dict(schema_version=2, research_only=True, execution_eligible=False, point_in_time_verified=False,
                greeks_assumption_status="experimental_unverified", risk={"status": "UNKNOWN", "max_quantity": None},
                options=list(reversed(options)), underlyings=bases, diagnostics=diagnostics,
                limitations_fa=["زمان بورس <نامعلوم>"], capture=capture,
                summary=counts | dict(contract_count=len(options), underlying_count=len(bases), technical_available_count=1),
                reconciliation=dict(raw_paired_rows=24, raw_sides=48, empty_sides=1, diagnostic_only_sides=1,
                                    identified_unique_contracts=len(options),
                                    diagnostics_by_reason={"missing_option_id": 1}))


class CombinedCacheFixture(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.stream = self.root / "stream"
        entry = self.stream / "00000002"
        entry.mkdir(parents=True)
        for name in collector.FILES:
            (entry / name).write_text("fixture", encoding="utf-8")
        self.body = entry / "body.raw.json"
        self.history = self.root / "history.json"
        self.registry = self.root / "registry.json"
        for path in (self.history, self.registry):
            path.write_text("fixture", encoding="utf-8")
        registry = patch.object(research.evidence, "REGISTRY", self.registry)
        registry.start()
        self.addCleanup(registry.stop)
        self.cache = research.CombinedCache(self.stream, self.history)


class CombinedCacheTests(CombinedCacheFixture):
    def test_one_report_and_invalidation_for_body_history_and_registry(self):
        with patch.object(research.combined, "build_market_research_report", side_effect=lambda *a, **kw:
                          full_market_fixture()) as build:
            old = self.cache.get()
            self.assertIs(self.cache.get(), old)
            self.assertEqual(build.call_count, 1)
            build.assert_called_with(research.COMBINED_CAPTURE, stream=self.stream,
                                     legacy_archive=watch.DEFAULT_ARCHIVE, history_path=self.history)
            for index, path in enumerate((self.body, self.history, self.registry), 2):
                with self.subTest(path=path.name):
                    before = path.stat()
                    path.write_text("changed", encoding="utf-8")  # Same size, restore mtime.
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                    new = self.cache.get()
                    self.assertIsNot(new, old)
                    self.assertIs(self.cache.get(), new)
                    self.assertEqual(build.call_count, index)
                    old = new
            self.body.write_text("corrupt", encoding="utf-8")
            build.side_effect = watch.ArchiveError("invalid archive")
            self.assertIsNone(self.cache.get())
            self.assertIsNone(self.cache.report)
            build.side_effect = None
            build.return_value = full_market_fixture() | {"execution_eligible": True}
            self.assertIsNone(self.cache.get())
            self.assertIsNone(self.cache.report)

    def test_changes_during_build_are_not_cached_and_replay_code_requires_restart(self):
        def changing(*args, **kwargs):
            self.history.write_text("changed during replay", encoding="utf-8")
            return full_market_fixture()

        with patch.object(research.combined, "build_market_research_report", side_effect=changing) as build:
            self.assertIsNone(self.cache.get())
            self.assertIsNone(self.cache.key)
            build.side_effect = None
            build.return_value = full_market_fixture()
            self.assertIs(self.cache.get(), build.return_value)
            with patch.object(self.cache, "code_signature", return_value=("changed replay code",)):
                self.assertIsNone(self.cache.get())
                self.assertIsNone(self.cache.report)
                self.assertEqual(build.call_count, 2)


class CombinedResearchHTTPTests(CombinedCacheFixture):
    def setUp(self):
        super().setUp()
        self.fixture = full_market_fixture()
        self.store = Mock()
        self.store.recent.return_value = []
        self.store.current_fingerprints.return_value = {}
        self.store.detail.return_value = (None, [])
        self.build = self.start_patch(patch.object(research.combined, "build_market_research_report",
                                                  return_value=self.fixture))
        self.start_patch(patch("sqlite3.connect", side_effect=AssertionError("unexpected database access")))
        self.start_patch(patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")))
        self.start_patch(patch.object(collector, "fetch", side_effect=AssertionError("unexpected collection")))
        self.start_patch(patch("paper_trading.PaperTrading.buy", side_effect=AssertionError("unexpected order")))
        self.start_patch(patch("paper_trading.PaperTrading.sell", side_effect=AssertionError("unexpected order")))
        self.start_patch(patch.object(research.combined, "atomic_write", side_effect=AssertionError("unexpected write")))
        self.server = dashboard.DashboardServer(0, self.store, [], combined_cache=self.cache)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.stop)

    def start_patch(self, guard):
        self.addCleanup(guard.stop)
        return guard.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def http(self, path, method="GET", **headers):
        with closing(HTTPConnection(*self.server.server_address, timeout=3)) as connection:
            connection.request(method, path, headers=headers)
            with connection.getresponse() as response:
                return response.status, dict(response.getheaders()), response.read().decode()

    def get(self, **query):
        return self.http(dashboard.COMBINED_RESEARCH + ("?" + urlencode(query) if query else ""))

    def ids(self, html):
        return re.findall(r'<h3>.*?</h3><bdi dir="ltr">(?:CALL|PUT)</bdi>'
                          r'<bdi class="technical-value" dir="ltr">([0-9]+)</bdi>', html)

    def test_combined_route_is_explicit_and_does_not_touch_existing_routes_or_security(self):
        home = self.http("/")[2]
        self.assertEqual(home.count(f'href="{dashboard.COMBINED_RESEARCH}"'), 1)
        self.assertEqual(self.http("/preflight")[0], 200)
        self.assertEqual(self.http("/review/" + "0" * 32)[0], 404)
        self.build.assert_not_called()
        self.store.reset_mock()
        path = dashboard.COMBINED_RESEARCH
        self.assertEqual(self.http(path, Host="elsewhere.invalid")[0], 403)
        self.assertEqual(self.http(path, **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.http(path, Origin="null")[0], 403)
        self.assertEqual(self.http(path, method="POST", Origin=self.server.origin)[0], 404)
        self.assertEqual(self.http(path, method="PUT")[0], 501)
        for query in ("capture=manual:3", "path=%2Ftmp", "q=a&q=b", "view=orders", "expiry=2026-02-30",
                      "page=0", "status=BUY_READY", "screening=untrusted", "liquidity=anything", "q=%FF"):
            with self.subTest(query=query):
                self.assertEqual(self.http(path + "?" + query)[0], 400)
        self.build.assert_not_called()
        self.assertEqual(self.store.mock_calls, [])
        status, headers, _ = self.http(path, **{"Sec-Fetch-Site": "none"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.store.mock_calls, [])

    def test_combined_cards_counts_labels_and_persian_bounded_pagination(self):
        snapshot = lambda: {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        original = snapshot()
        status, _, html = self.get()
        self.assertEqual(status, 200)
        for text in ('lang="fa" dir="rtl"', "manual:2", "دریافت HTTP در تهران (نه زمان بورس)", "ARCHIVED / NOT LIVE",
                     "point_in_time_verified=false", "experimental_unverified", "execution_eligible=false",
                     "risk.max_quantity=UNKNOWN", "اختیار یکتا<strong>", "۴۶", "۲۳", "RESEARCH_ONLY", "REJECTED",
                     "UNKNOWN", "فقط پژوهش؛ نه سیگنال", "غربال اولیه: عبور از غربال",
                     "رتبه‌بندی سخت‌گیرانه: رد در رتبه‌بندی", "&lt;rank&gt;", "&lt;script&gt;", "wide_spread",
                     "رد در غربال", "سمت خام", "missing_option_id"):
            self.assertIn(text, html)
        self.assertEqual(self.ids(html), [str(i) for i in range(90000, 90020)])
        self.assertNotIn("<script>", html)
        self.assertNotIn('method="post"', html)
        self.assertNotIn("/preflight", html)
        self.assertNotIn("/review/", html)
        first = self.get(kind="CALL", screening="passed", liquidity="observed", expiry="2027-01-20")[2]
        self.assertEqual(len(self.ids(first)), 20)
        next_url = unescape(re.search(r'<a href="([^"]+)">صفحهٔ بعد</a>', first)[1])
        params = parse_qs(urlsplit(next_url).query)
        self.assertEqual(params, {"kind": ["CALL"], "screening": ["passed"], "liquidity": ["observed"],
                                  "expiry": ["2027-01-20"], "view": ["options"], "page": ["2"]})
        self.assertEqual(self.ids(self.http(next_url)[2]), ["90040", "90042"])
        self.assertEqual(self.ids(self.get(page=99999)[2]), [str(i) for i in range(90040, 90046)])
        self.build.assert_called_once()
        self.assertEqual(self.store.mock_calls, [])
        self.assertEqual(snapshot(), original)

    def test_market_search_and_filters_keep_unknown_distinct(self):
        for term, ids in (("81000", ["90000", "90001"]), ("نام تازه", ["90000", "90001"]),
                          ("اختیار نمونه 90001", ["90001"]), ("90000", ["90000"])):
            with self.subTest(term=term):
                self.assertEqual(self.ids(self.get(q=term)[2]), ids)
        self.assertEqual(self.ids(self.get(status="RESEARCH_ONLY")[2]), ["90000"])
        self.assertEqual(self.ids(self.get(kind="PUT", status="REJECTED", screening="rejected", liquidity="limited",
                                           q="81022")[2]), ["90045"])
        missing = self.get(status="UNKNOWN", screening="unknown", liquidity="unknown", expiry="2027-01-21")[2]
        self.assertEqual(self.ids(missing), ["90044"])
        self.assertIn('bid مشاهده‌شده</dt><dd><bdi dir="ltr">UNKNOWN', missing)
        self.assertIn('تعداد / حجم معامله</dt><dd><bdi class="number" dir="ltr">۰', missing)
        self.assertIn("غربال اولیه: ارزیابی نشده", missing)
        self.assertIn("رتبه‌بندی سخت‌گیرانه: ارزیابی نشده", missing)
        self.build.assert_called_once()

    def test_all_base_categories_and_diagnostics_are_browsable_without_replay(self):
        first, second = (self.get(view="underlyings", page=p)[2] for p in (1, 2))
        self.assertEqual(first.count('class="candidate-card combined-underlying"'), 20)
        self.assertEqual(second.count('class="candidate-card combined-underlying"'), 3)
        self.assertIn("&lt;base-alias&gt;", first)
        self.assertIn("SMA(5)", first)
        self.assertIn("&lt;محدودیت&gt;", first)
        self.assertNotIn("SMA(5)", second)
        self.assertEqual(first.count("NOT_AVAILABLE") + second.count("NOT_AVAILABLE"), 22)
        for code in self.fixture["underlyings"]:
            self.assertIn(f'>{code}</bdi>', first + second)
        diag = self.get(view="diagnostics")[2]
        self.assertIn("ردیف خام", diag)
        self.assertIn("خارج از اختیارهای یکتا", diag)
        self.assertIn("missing_option_id", diag)
        self.assertIn("&lt;رکورد ناقص&gt;", diag)
        self.assertIn("سقف تعداد نامعلوم", diag)
        self.build.assert_called_once()

    def test_integrity_failure_returns_unavailable_not_previous_rows(self):
        self.assertEqual(self.get()[0], 200)
        self.body.write_text("tampered", encoding="utf-8")
        self.build.side_effect = watch.ArchiveError("sensitive path")
        status, _, html = self.get()
        self.assertEqual(status, 503)
        self.assertIn("دادهٔ پیشین نمایش داده نمی‌شود", html)
        self.assertNotIn("sensitive path", html)
        self.assertEqual(self.ids(html), [])
        self.assertIsNone(self.cache.report)
