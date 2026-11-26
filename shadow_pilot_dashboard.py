"""Read-only, loopback-only dashboard for the pilot's private report files."""

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import urlencode

from approval_dashboard import fields, h, ltr, metrics, page, select_input, shown
import dashboard_research as research
from market_data_quality import QUOTE_CLASSES
from paper_quotes import TEHRAN, timestamp
import shadow_collector as collector
from shadow_pilot import REASONS, configuration, status_for_root, verify_private_directory
from shadow_pilot_evidence import MissingPrivateFile, private_json
import shadow_watch as watch


HOST = "127.0.0.1"
MAX_REPORT = 4 * watch.MAX_BODY
TITLE = "داشبورد عملیاتی پایلوت سایه"
BANNER = "نمای فقط‌خواندنی پایلوت سایه و دفتر آزمایشی"
QUOTE_SHAPES = dict(zip(QUOTE_CLASSES, (
    "دوطرفه", "فقط خرید", "فقط فروش", "بدون مظنه", "قیمت خرید بالاتر از فروش", "ناسازگاری قیمت و عمق",
), strict=True))
HEALTH = {"ok": "فعال", "degraded": "نیازمند بررسی"}
POSITION_STATES = {"OPEN": "باز", "EXIT_PENDING": "در انتظار خروج", "EXPIRY_REVIEW": "در انتظار تسویه"}
CAPTURE_REASONS = {
    "no_listed_session": "روز معاملاتی در تقویم ثبت نشده",
    "outside_session_hours": "خارج از ساعت جلسه",
    "outside_collection_window": "خارج از پنجرهٔ گردآوری",
    "persistent_capture_rate_limit": "در انتظار فاصلهٔ مجاز بین درخواست‌ها",
    "duplicate body SHA-256": "محتوای تکراری",
    "collection_slot_changed": "پنجرهٔ گردآوری تغییر کرد",
}
STYLE = """
:root {--edge:light-dark(#c4d5d3,#345451);--panel:light-dark(#f0f8f6,#152c2a);
       --ink:light-dark(#10302d,#e3f4f1);--accent:light-dark(#086a60,#7edbc9)}
body{color:var(--ink);max-width:92rem}
a{color:var(--accent)}
.pilot-nav{display:flex;flex-wrap:wrap;gap:.4rem 1rem;margin-block:.8rem;font-weight:bold}
.pilot-nav a{min-height:unset}
.pilot-summary{background:var(--panel);border-color:var(--edge)}
.pilot-summary .cards>div{background:light-dark(white,#10221f);border-color:var(--edge);font-size:.95rem}
.pilot-summary .cards strong{font-size:1.1rem;margin-block:.2rem}
.pilot-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,24rem),1fr));gap:.7rem}
.pilot-grid>section{margin:0}
.pilot-table{overflow:auto}
.pilot-table table{width:100%;border-collapse:collapse;text-align:start;min-width:44rem}
.pilot-table th,.pilot-table td{text-align:start;padding:.55rem;border-block-end:1px solid var(--edge);overflow-wrap:anywhere}
.pilot-table thead{background:var(--panel)}
.pilot-table tr:last-child td{border-block-end:0}
.pilot-alert{border-inline-start:.25rem solid light-dark(#a04418,#efab6f);padding-inline:.65rem}
.pilot-muted{color:light-dark(#395b57,#a9c8c2);font-size:.9rem}
.pilot-alert li{overflow-wrap:anywhere}
"""


@dataclass(frozen=True)
class Filters:
    day: str = ""
    q: str = ""
    kind: str = ""
    status: str = "all"
    page: int = 1

    @classmethod
    def parse(cls, raw, today):
        values = research.query_values(raw, cls.__dataclass_fields__)
        day = values.get("day") or today
        if date.fromisoformat(day).isoformat() != day or day > today:
            raise ValueError("invalid report day")
        research.validate_search_text(values.get("q", ""))
        values.update(day=day, page=research.query_page(values.get("page", "1")))
        filters = cls(**values)
        if filters.kind not in ("", "CALL", "PUT") or filters.status not in research.STATUSES:
            raise ValueError("invalid market filter")
        return filters


class LegacyReport(ValueError):
    """A report published before the contract table was introduced."""


def read_report(root, day, now):
    reports = root / "reports"
    verify_private_directory(root)
    verify_private_directory(reports)
    report = private_json(reports / f"{day}.json", MAX_REPORT)
    if not isinstance(report, dict) or report.get("تاریخ تهران") != day or report.get("صلاحیت اجرای مشاهده") is not False:
        raise ValueError("unsafe report")
    generated = timestamp(report["زمان گزارش"])
    if generated > now or generated.astimezone(TEHRAN).date().isoformat() != day:
        raise ValueError("invalid report timestamp")
    observation = report["مشاهدات و غربال پژوهشی"]
    if observation is not None:
        if (isinstance(observation, dict) and "قراردادها" not in observation
                and {"آخرین ثبت", "پوشش"} <= observation.keys()):
            raise LegacyReport
        records = observation["قراردادها"]
        receipt = observation["آخرین ثبت"]
        if not isinstance(records, dict) or (bool(records) != (receipt is not None)):
            raise ValueError("invalid observation")
        if receipt is not None and (
            not isinstance(receipt, dict) or receipt["execution_eligible"] is not False or receipt["exchange_quote_timestamp"] is not None
            or timestamp(receipt["receipt_tehran"]) > generated
            or timestamp(receipt["receipt_tehran"]).astimezone(TEHRAN).date().isoformat() != day
        ):
            raise ValueError("unsafe observation evidence")
        for code, row in records.items():
            if (not watch.positive_id(code) or row["option_type"] not in ("CALL", "PUT")
                    or not watch.positive_id(row["underlying_code"]) or row["quote_shape"] not in QUOTE_SHAPES
                    or type(row["candidate_eligible"]) is not bool
                    or any(row[key] is not None and not isinstance(row[key], str)
                           for key in ("symbol", "underlying_symbol", "candidate_rejection"))):
                raise ValueError("invalid contract")
    return report, generated


def document(inner):
    return page(TITLE, inner, banner=BANNER, extra_style=STYLE)


def section(title, inner, *, anchor=""):
    return f'<section id="{anchor}"><h2>{h(title)}</h2>{inner}</section>'


def filters_form(filters, today):
    return (
        '<form class="filters" action="/" method="get">'
        f'<label>تاریخ گزارش تهران<input type="date" name="day" value="{h(filters.day)}" max="{h(today)}"></label>'
        f'<label>نماد یا شناسه<input name="q" maxlength="80" value="{h(filters.q)}"></label>'
        f'<label>نوع قرارداد{select_input("kind", {"": "همه", "CALL": "CALL", "PUT": "PUT"}, filters.kind)}</label>'
        f'<label>نتیجهٔ غربال{select_input("status", research.STATUSES, filters.status)}</label>'
        '<div class="filter-actions"><button type="submit">نمایش گزارش</button> <a href="/">امروز</a></div></form>'
    )


def table(headings, rows):
    head = "".join(f'<th scope="col">{h(cell)}</th>' for cell in headings)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="pilot-table" tabindex="0"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def market_section(observation, filters):
    if observation is None:
        return section("قراردادهای مشاهده‌شده", "<p>اعتبار بایگانی یا غربال قابل بررسی نیست.</p>", anchor="market")
    rows, total, current, pages = research.select_rows(observation["قراردادها"], research.Query(
        q=filters.q, kind=filters.kind, status=filters.status, page=filters.page, sort="symbol"
    ))
    body = table(("نماد و شناسه", "نوع و پایه", "اعمال / سررسید", "خرید / فروش", "عمق خرید / فروش",
                  "کیفیت مشاهده", "نتیجهٔ غربال"), [
        (f'{shown(row["symbol"])} · {ltr(code)}', f'{ltr(row["option_type"])} · {shown(row["underlying_symbol"])}',
         f'{shown(row["strike_price"])} / {ltr(row["maturity_date"])}',
         f'{shown(row["best_bid"])} / {shown(row["best_ask"])}',
         f'{shown(row["bid_volume"])} / {shown(row["ask_volume"])}', h(QUOTE_SHAPES[row["quote_shape"]]),
         "عبور از غربال" if row["candidate_eligible"] else h(REASONS.get(
             row["candidate_rejection"], row["candidate_rejection"] or "علت نامعلوم")))
        for code, row in rows
    ]) if rows else "<p>قراردادی برای این انتخاب وجود ندارد.</p>"
    links = []
    for label, number in (("صفحهٔ قبل", current - 1), ("صفحهٔ بعد", current + 1)):
        if 1 <= number <= pages:
            url = "/?" + urlencode(vars(filters) | {"page": number}) + "#market"
            links.append(f'<a href="{h(url)}">{label}</a>')
    return section("قراردادهای مشاهده‌شده",
                   '<p class="pilot-muted">مظنه‌های آرشیوی فقط برای پژوهش‌اند. قیمت و عمق به واحد خام منبع‌اند و زمان بورس و توالی سفارش‌ها تأیید نشده‌اند.</p>'
                   f'<p>{shown(total)} نتیجه · صفحهٔ {shown(current)} از {shown(pages)}</p>{body}'
                   f'<nav class="pager" aria-label="صفحه‌بندی">{"".join(links)}</nav>', anchor="market")


def paper_section(account, now, fresh):
    positions = account.get("موقعیت‌های ربات", [])
    rows = []
    for row in positions:
        valid_until = row.get("اعتبار ظرفیت تا UTC")
        capacity = row["ظرفیت فروش هر سفارش"]
        if row["وضعیت"] != "EXPIRY_REVIEW" and (not fresh or not valid_until or now >= timestamp(valid_until)):
            capacity = None
        rows.append((f'{shown(row["نماد"])} · {shown(row["شناسه"])}', ltr(row["نوع"]),
                     h(POSITION_STATES.get(row["وضعیت"], row["وضعیت"])), shown(row["تعداد باقی‌مانده"]),
                     shown(capacity) if capacity is not None else "نامعلوم",
                     "فعال" if row["هشدار حد ضرر"] is True else "آرام" if row["هشدار حد ضرر"] is False else "نامعلوم",
                     shown(row["شاهد لازم"])))
    body = table(("موقعیت", "نوع", "وضعیت", "تعداد باقی‌مانده", "ظرفیت فروش هر سفارش",
                  "حد ضرر", "شاهد لازم"), rows) if rows else "<p>موقعیت باز متعلق به این ربات در گزارش نیست.</p>"
    limits = account.get("حدود و ظرفیت خرید") or {}
    details = [(key, shown(value)) for key, value in limits.items()]
    if limits:
        details.append(("ظرفیت باقی‌ماندهٔ مواجهه", shown(account.get("ظرفیت باقی‌ماندهٔ مواجهه"))))
        details.append(("سقف فروش روزانه", "تعریف نشده" if account.get("سقف فروش روزانه") is None
                        else shown(account["سقف فروش روزانه"])))
    note = f'<p>{h(account.get("وضعیت", ""))} · توقف ورود {"فعال" if account.get("توقف ورود") else "غیرفعال"}</p>' if limits else f'<p>{h(account.get("وضعیت", ""))}</p>'
    if account.get("مانع"):
        note += f'<p class="pilot-alert">{h(account["مانع"])}</p>'
    if account.get("شواهد لازم"):
        note += "<ul>" + "".join(f"<li>{h(item)}</li>" for item in account["شواهد لازم"]) + "</ul>"
    exits = account.get("خروج‌های آزمایشی امروز", [])
    if exits:
        summary = table(("موقعیت", "نوع", "تعداد", "قیمت ریال", "زمان ثبت UTC"), [
            (shown(row["موقعیت"]), ltr(row["نوع"]), shown(row["تعداد"]), shown(row["قیمت ریال"]),
             raw_time(row["زمان UTC"])) for row in exits
        ])
        body += f"<details><summary>خروج‌های آزمایشی روز گزارش</summary>{summary}</details>"
    if limits and not fresh:
        note += '<p class="pilot-muted">ظرفیت خرید در زمان گزارش ثبت شده است.</p>'
    return section("دفتر آزمایشی و حدود", note + metrics(details) + body, anchor="paper")


def health_section(health, capture, observation, evidence, generated):
    state = health.get("سلامت", {})
    receipt = observation.get("آخرین ثبت") if observation else None
    fields_ = [
        ("سلامت فعلی زمان‌بندی", h(HEALTH.get(health["وضعیت"], health["وضعیت"]))),
        ("زمان گزارش تهران", ltr(generated.astimezone(TEHRAN).isoformat())),
        ("آخرین اجرای کامل UTC", raw_time(state.get("last_finished"))),
        ("آخرین تلاش گردآوری UTC", raw_time(state.get("last_attempt"))),
        ("آخرین گردآوری موفق UTC", raw_time(state.get("last_capture"))),
        ("وضعیت گردآوری", shown(capture.get("وضعیت"))),
        ("علت", shown(CAPTURE_REASONS.get(capture.get("علت"), capture.get("علت")))),
        ("دریافت HTTP تهران", raw_time(receipt["receipt_tehran"] if receipt else None)),
        ("زمان معتبر مظنه در بورس", "تأیید نشده"),
        ("شاهد اجرایی یا تسویهٔ مستقل در رجیستری",
         shown(evidence.get("شاهد اجرایی یا تسویهٔ مستقل تأییدشده در رجیستری"))),
    ]
    return section("زمان‌بندی و شواهد", fields(fields_), anchor="health")


def raw_time(value):
    return ltr(value) if value else "—"


def alerts_section(report):
    alerts = [f'{item.get("وضعیت", "")} · {item.get("علت") or item.get("مانع") or ""}'
              for item in report["هشدارهای عملیاتی"]]
    alerts += [f'{item["incident_key"]} · {item["detail"]} · {item["opened_at"]}'
               for item in report["دفتر آزمایشی"].get("هشدارهای بحرانی دفتر", [])]
    body = ('<ul class="pilot-alert">' + "".join(f"<li>{h(item)}</li>" for item in alerts) + "</ul>"
            if alerts else "<p>هشدار بحرانی در این گزارش ثبت نشده است.</p>")
    return section("هشدارهای بحرانی", body, anchor="alerts")


def dashboard(report, generated, health, filters, now, today):
    observation = report["مشاهدات و غربال پژوهشی"]
    coverage = observation["پوشش"] if observation is not None else {}
    fresh = filters.day == today and now - generated <= timedelta(minutes=3) and health["وضعیت"] in HEALTH
    lead = '<nav class="pilot-nav" aria-label="بخش‌های داشبورد"><a href="#market">قراردادها</a><a href="#paper">دفتر آزمایشی</a><a href="#alerts">هشدارها</a><a href="#health">سلامت زمان‌بندی</a></nav>'
    coverage_values = [("ثبت‌های این روز", shown(observation["تعداد ثبت روز"] if observation else None))]
    for kind in ("CALL", "PUT"):
        values = coverage.get(kind, {})
        for key in ("قرارداد", "نامزد", "دوطرفه"):
            coverage_values.append((f"{kind} · {key}", shown(values.get(key))))
    cards = "".join(f"<div>{h(label)}<strong>{value}</strong></div>" for label, value in coverage_values)
    reasons = observation["دلایل رد غربال"] if observation else {}
    breakdown = fields([(label, shown(count)) for label, count in reasons.items()]) if reasons else ""
    report_age = "" if fresh else "<p class='pilot-alert'>این گزارش تاریخی یا قدیمی است.</p>"
    return document(
        lead + f'<section class="pilot-summary"><h2>پوشش آخرین مشاهدهٔ روز</h2><div class="cards">{cards}</div>'
        + report_age + filters_form(filters, today)
        + (f"<details><summary>دلایل رد در کل مشاهده</summary>{breakdown}</details>" if breakdown else "") + "</section>"
        + '<div class="pilot-grid">' + health_section(health, report["گردآوری"], observation,
                                                     report["پوشش شواهد واقعی"], generated)
        + alerts_section(report) + "</div>" + market_section(observation, filters)
        + paper_section(report["دفتر آزمایشی"], now, fresh)
    )


class PilotServer(HTTPServer):
    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def __init__(self, root, port, *, clock=collector.utc_now):
        self.root, self.clock = root, clock
        super().__init__((HOST, port), PilotHandler)
        self.authority = f"{HOST}:{self.server_port}"


class PilotHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_):
        pass

    def respond(self, status, body=b"", *, allow=False):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if allow:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def unavailable(self, message, health, filters, today):
        status = health["وضعیت"]
        body = document(
            f'<p class="pilot-alert">{h(message)} وضعیت فعلی زمان‌بندی {h(HEALTH.get(status, status))} است.</p>'
            + filters_form(filters, today)
        )
        return self.respond(503, body)

    def do_GET(self):
        if (self.headers.get_all("Host") != [self.server.authority]
                or self.headers.get_all("Origin") not in (None, [f"http://{self.server.authority}"])
                or self.headers.get_all("Sec-Fetch-Site") not in (None, ["same-origin"], ["none"])):
            return self.respond(403, document("<p>مبدأ یا میزبان درخواست معتبر نیست.</p>"))
        path, _, raw = self.path.partition("?")
        if not path == "/":
            return self.respond(404, document("<p>صفحه پیدا نشد.</p>"))
        now = self.server.clock()
        today = now.astimezone(TEHRAN).date().isoformat()
        try:
            filters = Filters.parse(raw, today)
        except ValueError:
            return self.respond(400, document("<p>پارامتر گزارش نامعتبر است.</p>"))
        try:
            health = status_for_root(self.server.root, now)
            if not isinstance(health["وضعیت"], str):
                raise ValueError("invalid scheduler status")
        except (OSError, ValueError, TypeError, KeyError):
            health = {"وضعیت": "وضعیت زمان‌بند در دسترس نیست"}
        try:
            report, generated = read_report(self.server.root, filters.day, now)
            body = dashboard(report, generated, health, filters, now, today)
        except (MissingPrivateFile, FileNotFoundError):
            return self.unavailable("گزارش این تاریخ هنوز منتشر نشده است.", health, filters, today)
        except LegacyReport:
            return self.unavailable("نسخهٔ قدیمی گزارش جدول قراردادها را ندارد.", health, filters, today)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            return self.unavailable("گزارش این تاریخ نامعتبر است.", health, filters, today)
        self.respond(200, body)

    do_HEAD = do_GET

    def reject_write(self):
        self.respond(405, allow=True)

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = reject_write


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be in 1..65535")
    try:
        root, _, _, _, _ = configuration(args.config)
        with PilotServer(root, args.port) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        parser.exit(1, f"shadow dashboard: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
