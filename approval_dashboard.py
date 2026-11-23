"""Persian localhost dashboard for archived TSE/IFB observations and research review."""

import argparse
from datetime import date, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import secrets
from socketserver import TCPServer
import sqlite3
import sys
import unicodedata
from urllib.parse import parse_qs, urlencode

from approval_workflow import ALLOCATION_PERCENT, HOLD_DAYS, ApprovalStore, DECISIONS, utc_now
import dashboard_research as research
from paper_quotes import MAX_SQLITE_INT, timestamp
import shadow_collector as collector
import shadow_multiday
import shadow_watch as watch


HOST = "127.0.0.1"
DB = watch.ROOT / "data/approval-dashboard/approvals.sqlite3"
REVIEW = re.compile(r"/review/([0-9a-f]{32})")
PREFLIGHT = "/preflight"
COMBINED_RESEARCH = "/research/combined"
MAX_FORM = 1024
PREFLIGHT_MAX_FORM = 2048
PREFLIGHT_LOSS_PERCENT = 2
MANUAL_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
PREFLIGHT_PRICE_UNITS = {
    "rial_unit": ("ریال به‌ازای هر واحد پایه", 1, True),
    "toman_unit": ("تومان به‌ازای هر واحد پایه", 10, True),
    "rial_contract": ("ریال به‌ازای هر قرارداد", 1, False),
    "toman_contract": ("تومان به‌ازای هر قرارداد", 10, False),
}
PREFLIGHT_STRIKE_UNITS = {"rial": "ریال به‌ازای هر واحد پایه", "toman": "تومان به‌ازای هر واحد پایه"}
PREFLIGHT_UNIT_SOURCES = {"broker_screen": "ورود دستی از صفحهٔ ایزی‌تریدر", "operator_stated": "واحد به گفتهٔ اپراتور"}
PREFLIGHT_SETTLEMENT_DATES = {
    "cash_settlement_date": "تاریخ تسویهٔ نقدی",
    "physical_settlement_date": "تاریخ تسویهٔ فیزیکی",
}
PREFLIGHT_EVIDENCE_FIELDS = {"observed_strike_unit", "strike_unit_source", *PREFLIGHT_SETTLEMENT_DATES}
PREFLIGHT_NUMBERS = {
    "strike": "قیمت اعمال",
    "contract_size": "اندازهٔ قرارداد (تعداد واحد پایه در هر قرارداد)",
    "limit_price": "قیمت محدود خرید",
    "quantity": "تعداد قرارداد",
    "fee": "کارمزد کل خرید اعلام‌شده (ریال)",
    "initial_equity": "سرمایهٔ اولیه (ریال)",
    "committed": "بهای خرید موقعیت‌های باز (ریال)",
    "reserved": "رزرو سفارش‌های باز و سایر تعهدها، جدا از بهای موقعیت‌های باز (ریال)",
    "cash": "وجه نقد در دسترس پس از کسر رزروها و تعهدها (ریال)",
    "max_loss": "حداکثر زیان کل قابل‌قبول برای این معامله (ریال)",
}
PREFLIGHT_SIZING_NUMBERS = {
    "fee_cap": "کران مجموع کارمزد و هزینه‌های الزامی خرید (ریال به‌ازای هر قرارداد)",
    "ask_depth": "عمق فروش مستقل بررسی‌شده تا قیمت محدود (تعداد قرارداد؛ اختیاری)",
}
PREFLIGHT_FEE_SOURCES = {
    "broker": "مستند کارگزار برای همهٔ تعدادهای مورد بررسی",
    "official": "تعرفهٔ رسمیِ قابل‌اعمال برای همهٔ تعدادهای مورد بررسی",
}
# Preflight estimates purchases only; no side or action field is accepted.
PREFLIGHT_FIELDS = {"instrument", "symbol", "kind", "expiry", "strike_unit", "price_unit", "cash_unit",
                    "fee_cap_source", "fee_cap_ref", "sizing_inputs_checked",
                    *PREFLIGHT_NUMBERS, *PREFLIGHT_SIZING_NUMBERS}
PREFLIGHT_UNIT_LABELS = {
    "strike_unit": "واحد قیمت اعمال",
    "price_unit": "واحد و مبنای قیمت محدود خرید (پریمیوم)",
    "cash_unit": "واحد کارمزد کل، سرمایهٔ اولیه، تعهدها و وجه نقد در دسترس",
}
MANUAL_SYMBOL = re.compile(
    r"[A-Za-z\u0621-\u063a\u0641-\u064a\u067e\u0686\u0698\u06a9\u06af\u06cc]"
    r"[A-Za-z0-9_\u0621-\u063a\u0640-\u064a\u067e\u0686\u0698\u06a9\u06af\u06cc\u200c-]{0,39}"
)
STATES = {
    "PENDING": "در انتظار بررسی", "APPROVED": "تأیید پژوهشی", "REJECTED": "رد شد",
    "EXPIRED": "منقضی", "CANCELLED": "لغو شد",
}
CANDIDATE_REASONS = {
    "invalid_quote_or_closing_spot": "مظنه یا قیمت پایانی پایه نامعتبر",
    "insufficient_trades": "تعداد معاملات ناکافی", "no_trade_volume": "بدون حجم معامله",
    "no_bid_depth": "بدون عمق خرید", "no_ask_depth": "بدون عمق فروش",
    "near_expiry": "سررسید نزدیک", "wide_spread": "اسپرد زیاد",
    "ask_below_intrinsic": "قیمت فروش کمتر از ارزش ذاتی",
}
GREEKS_STATUSES = {
    "experimental_unverified": "آزمایشی، تأیید نشده",
    "validated": "فرض‌های مدل اعتبارسنجی شده‌اند",
}
IV_STATUSES = {
    "ok": "محاسبه شده، تأیید نشده",
    "invalid_input": "ورودی نامعتبر",
    "mid_unavailable_ask_available": "IV میانی ناموجود؛ IV فروش تأیید نشده",
    "mid_unavailable": "IV میانی ناموجود",
}
FA_NUMBERS = str.maketrans("0123456789,.-", "۰۱۲۳۴۵۶۷۸۹٬٫−")
EVENT_REASONS = {
    "eligible_snapshot": "ثبت جزئیات واجد شرایط",
    "deadline_elapsed": "پایان اعتبار",
    "eligibility_withdrawn_or_changed": "تغییر جزئیات یا از دست رفتن شرایط",
    "explicit_paper_research_decision": "تصمیم صریح، فقط برای پژوهش",
}


def h(value):
    return escape(str(value))


def ltr(value):
    return f'<bdi dir="ltr">{h(value)}</bdi>'


def phase1_eligible():
    # Real archived responses are unverified. No independently verified quote,
    # model/contract provider or risk adapter is commissioned in Phase 1.
    # Never accept ranked files, browser input or synthetic fixtures here.
    return ()


def archive_summary(stream=collector.STREAM, legacy=watch.DEFAULT_ARCHIVE):
    watch.require(not legacy.is_symlink(), "unsafe legacy archive")
    report = shadow_multiday.build_report(stream, legacy=legacy if os.path.lexists(legacy) else None)
    return summary_cards(report)


def summary_cards(report):
    days = sorted(report["days"].items(), reverse=True)[:7]
    result = []
    for day, data in days:
        states = data["contracts"].values()
        card = {key: value for key, value in data.items() if key != "contracts"}
        card.update(day=day, two_sided_count=sum(row["two_sided"] for row in states),
                    candidate_count=sum(row["candidate_eligible"] for row in states))
        result.append(card)
    return result


def page(title, inner):
    return f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)} | TSE/IFB</title>
<style>
:root{{color-scheme:light dark}} *{{box-sizing:border-box}}
 body{{font:1rem/1.55 Tahoma,"Noto Sans Arabic",system-ui,sans-serif;margin:auto;padding:clamp(.55rem,2vw,1.2rem);max-width:88rem}}
 section{{border:1px solid #818181;border-radius:.65rem;padding:clamp(.55rem,2vw,1rem);margin:.65rem 0}}
 h1,h2,h3,p{{margin:.25rem 0 .5rem}} h2{{font-size:1.25rem}} h3{{font-size:1.05rem}}
 dt{{font-weight:bold}} dd{{margin:0 0 .4rem}}
 summary{{cursor:pointer;padding-block:.6rem}} summary,a,button{{min-height:44px}}
 bdi,dd,summary,.candidate-status{{overflow-wrap:anywhere}}
 .number{{font-variant-numeric:tabular-nums}}
 .technical-value{{font-family:ui-monospace,monospace;font-size:.9rem;white-space:pre-wrap;overflow-wrap:anywhere}}
 button,input,select{{font:inherit;padding:.35rem .5rem;min-height:44px}}
 input[type=checkbox]{{width:1.35rem;height:1.35rem;min-height:0}}
 .cards,.filters{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,8.5rem),1fr));gap:.4rem;margin:.45rem 0}}
 .cards>div{{border:1px solid #818181;border-radius:.5rem;padding:.3rem .5rem;font-size:.9rem;overflow-wrap:anywhere}}
 .cards strong,.filters label{{display:block}} .filters input,.filters select{{width:100%;min-width:0}}
 .filter-actions{{grid-column:1/-1}} .filter-actions a{{display:inline-flex;align-items:center;padding-inline:.45rem}}
 .candidate-grid{{display:grid;grid-template-columns:minmax(0,1fr);gap:.5rem;align-items:start}}
 .candidate-card{{border:1px solid #818181;border-radius:.55rem;padding:.45rem .6rem 0;min-width:0}}
 .candidate-title,.candidate-status{{display:flex;flex-wrap:wrap;align-items:baseline;gap:.15rem .55rem}}
 .candidate-title h3,.candidate-status p{{margin:0}}
 .candidate-status{{font-size:.9rem}}
 .metrics{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.25rem .6rem;margin:.35rem 0}}
 .metrics dt{{font-size:.875rem;font-weight:normal}} .metrics dd{{margin:0;font-weight:bold}}
 .record-details{{border-block-start:1px solid #818181;padding-block:.45rem}}
 .record-details dl:not(.metrics){{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,3fr);gap:.25rem .5rem;margin:.4rem 0}}
 .record-details dt,.record-details dd{{margin:0;overflow-wrap:anywhere}}
 .candidate-toolbar{{display:flex;gap:0 .8rem;flex-wrap:wrap;align-items:center}}
 .candidate-toolbar a{{display:inline-flex;align-items:center}}
 .pager{{display:flex;gap:.8rem;flex-wrap:wrap;align-items:center}} .pager:empty{{display:none}}
 .pager a{{display:inline-flex;align-items:center}} .badge{{display:inline-block;border:1px solid;border-radius:.3rem;padding:0 .35rem}}
 .rejected{{border-width:2px;font-weight:bold}}
 .contract-comparison{{width:100%;table-layout:fixed;border-collapse:collapse;text-align:start;overflow-wrap:anywhere;font-size:.9rem}}
 .contract-comparison th,.contract-comparison td{{border-block-end:1px solid #818181;padding:.25rem;text-align:start}}
 .preflight-warning{{border-inline-start:.25rem solid;padding-inline:.6rem;overflow-wrap:anywhere}}
 .preflight-warning ul,.preflight-checklist ol{{padding-inline-start:1.4rem}}
 .preflight-warning li,.preflight-checklist li{{margin-block:.35rem}}
 .contract-comparison th:last-child,.contract-comparison td:last-child{{width:26%;font-size:.82rem}}
 @media (min-width:26rem){{
   .metrics{{grid-template-columns:repeat(4,minmax(0,1fr))}}
 }}
 @media (min-width:64rem) and (orientation:landscape){{
   .candidate-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}
 }}
</style></head><body><main><h1>{h(title)}</h1>
<p><strong>فقط پژوهش کاغذی — بدون سفارش، پرشدن یا تغییر دفتر معاملات.</strong></p>
{inner}</main></body></html>""".encode("utf-8")


def fields(values):
    return "<dl>" + "".join(f"<dt>{h(label)}</dt><dd>{value}</dd>" for label, value in values) + "</dl>"


def metrics(values):
    return '<dl class="metrics">' + "".join(
        f"<div><dt>{h(label)}</dt><dd>{value}</dd></div>" for label, value in values
    ) + "</dl>"


def reasons(title, values, labels):
    items = "".join(f"<li>{h(labels.get(key, key))}: {shown(count)}</li>" for key, count in
                    sorted(values.items(), key=lambda item: (-item[1], item[0])))
    return f"<details><summary>{h(title)}</summary>{'<ul>' + items + '</ul>' if items else 'ردی ثبت نشده است.'}</details>"


def visible_state(row, now):
    if row["state"] == "PENDING" and now >= timestamp(row["valid_until"]):
        return "EXPIRED"
    return row["state"]


OBSERVATIONAL = "صرفاً مقایسهٔ مشاهداتی؛ نه سود، نه بازده قابل اجرا و نه مظنهٔ لحظه‌ای."
COMBINED_WARNING = (
    '<p class="preflight-warning"><strong>مشاهدهٔ آرشیوی، نه دادهٔ زنده '
    '(<bdi dir="ltr">ARCHIVED / NOT LIVE</bdi>)؛ نه سیگنال یا پیشنهاد معامله.</strong><br>'
    'دسترسی تاریخی تأیید نشده (<bdi dir="ltr">point_in_time_verified=false</bdi>)؛ '
    'فرض‌های Greeks آزمایشی (<bdi dir="ltr">experimental_unverified</bdi>)؛ '
    'غیرقابل اجرا (<bdi dir="ltr">execution_eligible=false</bdi>)؛ '
    'سقف تعداد نامعلوم (<bdi dir="ltr">risk.max_quantity=UNKNOWN</bdi>).</p>'
)
COMPARISON_LABELS = {
    "best_bid": "خرید", "best_ask": "فروش", "spread_percent": "اسپرد (واحد درصد)",
    "bid_volume": "عمق خرید", "ask_volume": "عمق فروش", "trade_count": "تعداد معاملات",
    "trade_volume": "حجم معاملات",
}
UNAVAILABLE = {
    "absent": "شناسه در ثبت دیروز یافت نشد.",
    "identity": "فیلدهای هویت قرارداد مطابقت ندارند.",
    "quote": "حداقل یکی از دو ثبت، قیمت و عمق دوطرفهٔ مناسب ندارد.",
}


def shown(value, *, signed=False, suffix=""):
    if research.numeric(value):
        digits = f"{value:,}" if type(value) is int else f"{value:,.6f}".rstrip("0").rstrip(".")
        if digits == "-0":
            digits = "0"
        if signed and not digits.startswith("-") and digits != "0":
            digits = "+" + digits
        return f'<bdi class="number" dir="ltr">{digits.translate(FA_NUMBERS)}{h(suffix)}</bdi>'
    return f"<bdi>{h(value)}</bdi>" if isinstance(value, str) and value.strip() else "—"


def raw(value):
    return f'<bdi class="technical-value" dir="ltr">{h(value)}</bdi>' if value is not None else "—"


def display_date(value):
    if not value:
        return "—"
    return ltr(date.fromisoformat(value).isoformat().replace("-", "/").translate(FA_NUMBERS))


def display_receipt(value):
    # Preserve the stored Tehran offset; timestamp() normalizes to UTC.
    received = datetime.fromisoformat(value)
    return f'{display_date(received.date().isoformat())}، ساعت {ltr(received.strftime("%H:%M:%S").translate(FA_NUMBERS))}'


def receipt_details(observation):
    return [
        ("شناسهٔ ثبت", raw(observation["capture_id"])),
        ("هش SHA-256 بدنهٔ آرشیو", raw(observation["body_sha256"])),
        ("زمان خام دریافت HTTP (تهران)", raw(observation["receipt_tehran"])),
        ("زمان خام دریافت HTTP (UTC)", raw(observation["receipt_utc"])),
    ]


def status_text(value, labels):
    return labels.get(value, "وضعیت نامشخص") if value else "ارزیابی نشده"


def card_grid(values):
    return '<div class="cards">' + "".join(
        f"<div>{h(label)}<strong>{shown(value)}</strong></div>" for label, value in values
    ) + "</div>"


def comparison_section(comparison):
    heading = '<section id="daily-comparison"><h2>مقایسهٔ روزانه</h2>'
    if not comparison or not comparison[1]:
        day = comparison[0] if comparison else None
        return (heading +
                f"<p>{OBSERVATIONAL}</p><p>ثبت متفاوت و مناسب برای روز قبل ({display_date(day)}) در دسترس نیست.</p></section>")
    _, receipt, _, counts = comparison
    return (heading + f"<p>{OBSERVATIONAL}</p>"
            f"<p>کل واچ‌لیست در برابر آخرین دریافت روز قبلِ تهران: {display_receipt(receipt)}. "
            "این دو ثبت، پایان جلسه یا پوشش کامل بازار را ثابت نمی‌کنند. "
            "تطبیق هویت فقط درون آرشیو است، نه تأیید مستقل بورس.</p>"
            + card_grid([
                ("قابل مقایسه", counts["observed"]), ("تغییر قیمت", counts["price_changed"]),
                ("تغییر اسپرد", counts["spread_changed"]), ("تغییر نقدشوندگی", counts["liquidity_changed"]),
                ("دارای هر دو شمارندهٔ معامله", counts["trade_counters_available"]),
                ("شناسهٔ فقط امروز", counts["absent"]), ("شناسهٔ فقط دیروز", counts["disappeared"]),
                ("هویت نامنطبق", counts["identity"]), ("دوطرفهٔ نامناسب", counts["quote"]),
            ]) + "</section>")


def comparison_details(comparison, code):
    prefix = f"<h3>اختلاف با روز قبل</h3><p>{OBSERVATIONAL}</p>"
    if not comparison or not comparison[1]:
        return prefix + "<p>ثبت مناسب برای مقایسه در دسترس نیست.</p>"
    status, changes = comparison[2][code]
    if status != "observed":
        return prefix + f"<p>{h(UNAVAILABLE[status])}</p>"
    return prefix + metrics([
        (label, shown(changes[key], signed=True) if key in changes else "ناموجود")
        for key, label in COMPARISON_LABELS.items()
    ]) + "<p>اختلاف شمارنده‌های گزارش‌شده است، نه فعالیت بین دو ثبت.</p>"


def candidate_card(code, row, latest, comparison):
    passed = row["candidate_eligible"]
    badge = ('<span class="badge">نامزد بررسی اولیه</span>'
             if passed else '<span class="badge rejected">رد در غربال</span>')
    rejection = row["candidate_rejection"]
    rank_rejection = row["rank_rejection"]
    if not passed:
        reason = h(CANDIDATE_REASONS.get(rejection, "علت نامشخص" if rejection else "علتی ثبت نشده"))
    elif rank_rejection:
        reason = f"اولین رد رتبه‌بندی: {shown(rank_rejection)}"
    else:
        reason = ""
    details = fields([
        ("کد رد غربال اولیه", raw(rejection)),
        ("رتبه‌بندی", shown(rank_rejection) if rank_rejection else
         ("عبور از قواعد رتبه‌بندی؛ نه پیشنهاد" if row["rank_eligible"] else "ارزیابی نشده")),
        ("وضعیت خام فرض‌های Greeks", raw(row["greeks_assumption_status"])),
        ("وضعیت خام IV", raw(row["iv_status"])),
        ("علت ثبت‌شدهٔ شکست IV میانی", raw(row.get("iv_failure_reason"))),
        ("شناسهٔ قرارداد (insCode)", raw(code)),
        ("شناسهٔ پایه", raw(row["underlying_code"])),
        ("آخرین معامله / پایانی پایه (واحد خام)", f'{shown(row["underlying_last_price"])} / {shown(row["underlying_closing_price"])}'),
        ("قیمت اعمال (واحد خام)", shown(row["strike_price"])),
        ("تاریخ سررسید گزارش‌شده (میلادی)", display_date(datetime.strptime(row["maturity_date"], "%Y%m%d").date().isoformat())),
        ("تاریخ خام سررسید", raw(row["maturity_date"])),
        ("اندازهٔ قرارداد / روز باقی‌ماندهٔ گزارش‌شده", f'{shown(row["contract_size"])} / {shown(row["remaining_days"])}'),
        ("عمق خرید / فروش و حجم معامله", f'{shown(row["bid_volume"])} / {shown(row["ask_volume"])} · {shown(row["trade_volume"])}'),
        ("نشانی کامل منبع", raw(watch.SOURCE)),
        *receipt_details(latest),
    ])
    base = shown(row.get("underlying_symbol"))
    greeks = status_text(row["greeks_assumption_status"], GREEKS_STATUSES)
    iv = status_text(row["iv_status"], IV_STATUSES)
    quotes = metrics([
        ("خرید", shown(row["best_bid"])), ("فروش", shown(row["best_ask"])),
        ("اسپرد", shown(row["spread_percent"], suffix="٪")), ("تعداد معامله", shown(row["trade_count"])),
    ])
    return (f'<article class="candidate-card"><header class="candidate-title"><h3>{shown(row["symbol"])}</h3>'
            f'{ltr(row["option_type"])}<span>پایه: {base}</span></header>{quotes}'
            f'<div class="candidate-status">{badge}{f"<p>{reason}</p>" if reason else ""}'
            '<strong>غیرقابل اجرا</strong></div>'
            f'<div class="candidate-status"><span>فرض‌های <bdi dir="ltr">Greeks</bdi>: {h(greeks)}</span>'
            f'<span><bdi dir="ltr">IV</bdi>: {h(iv)}</span></div>'
            '<details class="candidate-details"><summary>جزئیات فنی و مقایسه</summary><div class="record-details">'
            + comparison_details(comparison, code) + f'<h3>فرادادهٔ فنی قرارداد و ثبت</h3>{details}'
            '<p>زمان و توالی مظنه، هم‌زمانی قیمت پایه و مهلت معاملهٔ قرارداد نامعلوم‌اند. '
            'شرط‌های پس از اولین رد لزوماً ارزیابی نشده‌اند.</p></div></details></article>')


def select_input(name, choices, value):
    return f'<select name="{name}">' + "".join(
        f'<option value="{h(key)}"{" selected" if key == value else ""}>{h(label)}</option>'
        for key, label in choices.items()
    ) + "</select>"


def candidate_section(latest, comparison, query):
    rows, total, page_no, pages = research.select_rows(latest["contracts"], query)
    controls = [
        ("جست‌وجوی نماد یا شناسه", f'<input name="q" maxlength="80" value="{h(query.q)}">'),
        ("نوع اختیار", select_input("kind", {"": "همه", "CALL": "CALL", "PUT": "PUT"}, query.kind)),
        ("نماد دقیق پایه", f'<input name="underlying" maxlength="80" value="{h(query.underlying)}">'),
        ("حداکثر اسپرد (٪)", f'<input name="spread" type="number" min="0" max="200" step="any" value="{h(query.spread)}">'),
        ("وضعیت غربال", select_input("status", research.STATUSES, query.status)),
        ("مرتب‌سازی", select_input("sort", {k: label for k, (_, label) in research.SORTS.items()}, query.sort)),
        ("ترتیب", select_input("order", {"asc": "صعودی", "desc": "نزولی"}, query.order)),
    ]
    form = '<form class="filters" method="get" action="/#candidates">' + "".join(
        f"<label>{label}{control}</label>" for label, control in controls
    ) + '<div class="filter-actions"><button type="submit">اعمال فیلتر</button> <a href="/#candidates">پاک‌کردن</a></div></form>'
    filters_active = query != research.Query(page=query.page)
    control_label = "جست‌وجو، فیلتر و مرتب‌سازی" + (" (فعال)" if filters_active else "")
    cards = ('<div class="candidate-grid">' + "".join(candidate_card(code, row, latest, comparison)
                                                        for code, row in rows) + "</div>") if rows else (
        "<p>قراردادی مطابق فیلترها یافت نشد.</p>")
    links = []
    for number, label in ((page_no - 1, "صفحهٔ قبل"), (page_no + 1, "صفحهٔ بعد")):
        if 1 <= number <= pages:
            href = "/?" + urlencode(vars(query) | {"page": number}) + "#candidates"
            links.append(f'<a href="{h(href)}">{label}</a>')
    return (f'<section id="candidates"><h2>غربال اولیهٔ آخرین مشاهده</h2>'
            '<p>نامزد بررسی اولیه، پیشنهاد معامله یا درخواست تأیید نیست. قیمت‌ها در واحد خام منبع‌اند؛ واحد تأیید نشده است.</p>'
            f'<details><summary>{control_label}</summary>{form}</details>'
            f'<div class="candidate-toolbar"><p>{shown(total)} نتیجه · صفحهٔ {shown(page_no)} از {shown(pages)}</p>'
            f'<a href="#daily-comparison">خلاصهٔ مقایسهٔ روزانه</a></div>{cards}'
            f'<nav class="pager" aria-label="صفحه‌بندی">{" ".join(links)}</nav></section>')


def combined_value(value, *, suffix=""):
    return ltr("UNKNOWN") if value is None else shown(value, suffix=suffix)


def combined_option_card(row):
    screen = row.get("screening") or {}
    rejection, rank_rejection = screen.get("candidate_rejection"), screen.get("rank_rejection")
    screened = research.combined_screening(row)
    screening = research.COMBINED_SCREENING[screened]
    if rejection:
        screening += " — " + CANDIDATE_REASONS.get(rejection, "علت در دادهٔ پژوهشی ثبت شده است")
    if rank_rejection:
        rank = "رد در رتبه‌بندی — " + rank_rejection
    elif screen.get("rank_eligible") is True:
        rank = "عبور از قواعد رتبه؛ نه پیشنهاد یا مجوز"
    else:
        rank = "ارزیابی نشده"
    expiry = row.get("maturity_date")
    expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}" if isinstance(expiry, str) and re.fullmatch(
        r"[0-9]{8}", expiry) else None
    details = fields([
        ("کد رد غربال", raw(rejection)),
        ("طبقهٔ خام مظنه", combined_value(row.get("quote_class"))),
        ("ایرادهای نقدشوندگی ثبت‌شده", raw("، ".join(row.get("liquidity_issues") or ()) or None)),
        ("وضعیت IV", raw(screen.get("iv_status"))),
        ("وضعیت مدل این ردیف", raw(screen.get("greeks_assumption_status"))),
        ("واحد قیمت / عمق / ضریب", " / ".join(combined_value(row.get(key))
                                              for key in ("price_unit", "depth_unit", "multiplier_unit"))),
        ("مهلت معامله / اعمال / تسویه", " / ".join(combined_value(row.get(key))
                                                  for key in ("trade_cutoff_status", "exercise_status",
                                                              "settlement_status"))),
    ])
    quotes = metrics([
        ("bid مشاهده‌شده", combined_value(row.get("best_bid"))), ("ask مشاهده‌شده", combined_value(row.get("best_ask"))),
        ("اسپرد", combined_value(row.get("spread_percent"), suffix="٪")),
        ("عمق bid / ask", f'{combined_value(row.get("bid_volume"))} / {combined_value(row.get("ask_volume"))}'),
        ("تعداد / حجم معامله", f'{combined_value(row.get("trade_count"))} / {combined_value(row.get("trade_volume"))}'),
        ("اعمال / ضریب", f'{combined_value(row.get("strike_price"))} / {combined_value(row.get("contract_size"))}'),
        ("تاریخ سررسید گزارش‌شده", display_date(expiry) if expiry else ltr("UNKNOWN")),
    ])
    status = row["status"]
    return ('<article class="candidate-card combined-option"><header class="candidate-title">'
            f'<h3>{combined_value(row.get("symbol"))}</h3>{ltr(row["option_type"])}{raw(row["ins_code"])}'
            f'<span>پایه: {combined_value(row.get("underlying_symbol"))} {raw(row["underlying_code"])}</span></header>'
            f'<p><strong>{h(research.COMBINED_STATUSES.get(status, "نامعلوم"))}</strong> · {ltr(status)} — '
            f'{h(row["reason_fa"])} {raw(row["reason_code"])}</p>{quotes}'
            f'<p>غربال اولیه: {h(screening)}<br>رتبه‌بندی سخت‌گیرانه: {h(rank)}</p>'
            f'<details><summary>جزئیات فنی مشاهده</summary><div class="record-details">{details}</div></details></article>')


def combined_underlying_card(code, group):
    technical = group["technical_analysis"]
    content = f'<p>{ltr(technical["status"])} — {h(technical["reason_fa"])} {raw(technical["reason_code"])}</p>'
    if technical["status"] == "AVAILABLE" and technical.get("history"):
        history = technical["history"]
        indicators = []
        for name, item in history["indicators"].items():
            value = item.get("value")
            if isinstance(value, dict):
                display = " / ".join(f"{raw(key)}: {combined_value(number)}" for key, number in value.items())
            else:
                display = combined_value(value)
            explanation = f' — {h(item["reason"])}' if item.get("reason") else ""
            warmup = f' · {raw(item["warmup"])}' if item.get("warmup") else ""
            indicators.append((name, f'{raw(item["status"])}: {display}{explanation}{warmup}'))
        content += fields([
            ("قیمت‌های پایانی قابل استفاده", combined_value(history["valid_close_count"])),
            ("تاریخ برش انحصاری", raw(history["session_date_cutoff_exclusive"])),
            ("تعدیل / کامل‌بودن جلسات", f'{raw(history["adjustment_status"])} / {raw(history["session_completeness"])}'),
        ]) + ("<p>تاریخچه پس از ثبت اختیار دریافت شده است.</p>" if technical.get("downloaded_after_capture") else "")
        content += '<details><summary>مشاهدات فنی — نه سیگنال</summary>' + fields(indicators) + "</details>"
    types = group["option_counts"]
    return (f'<article class="candidate-card combined-underlying"><h3>{shown("، ".join(group["symbols"]))} {raw(code)}</h3>'
            f'<p>CALL: {shown(types["CALL"])} · PUT: {shown(types["PUT"])}</p>{content}</article>')


def combined_diagnostic_card(row):
    scope = "خارج از اختیارهای یکتا" if row["excluded_from_options"] else "برای اختیار دارای هویت یکتا"
    return (f'<article class="candidate-card combined-diagnostic"><h3>ردیف خام {shown(row["raw_row"])} · {ltr(row["side"])}</h3>'
            f'<p>{raw(row["status"])} · {h(scope)} — {h(row["reason_fa"])} {raw(row["reason_code"])}</p>'
            + fields([("شناسهٔ اختیار", combined_value(row.get("ins_code"))),
                      ("شناسهٔ پایه", combined_value(row.get("underlying_code"))),
                      ("فیلدهای معیوب", raw("، ".join(row.get("fields", ())) or None))]) + "</article>")


def combined_page(report, query):
    capture, summary, counts = report["capture"], report["summary"], report["reconciliation"]
    types, states, screening = summary["option_counts"], summary["research_counts"], summary["screening"]
    totals = card_grid([
        ("اختیار یکتا", summary["contract_count"]), ("CALL", types["CALL"]), ("PUT", types["PUT"]),
        ("دارایی پایه", summary["underlying_count"]), ("پایه با تاریخچهٔ قابل استفاده", summary["technical_available_count"]),
        ("عبور از غربال اولیه", screening["candidate_passes"] if screening["evaluated"] else "ارزیابی نشده"),
        ("عبور از رتبه‌بندی سخت‌گیرانه", screening["strict_passes"] if screening["evaluated"] else "ارزیابی نشده"),
        *((label, states[label]) for label in research.COMBINED_STATUSES),
    ])
    diagnostics = (f'<p>ردیف خام: {shown(counts["raw_paired_rows"])} · سمت خام: {shown(counts["raw_sides"])} · '
                   f'سمت خالی: {shown(counts["empty_sides"])} · فقط تشخیصی: {shown(counts["diagnostic_only_sides"])} · '
                   f'اختیار با هویت یکتا: {shown(counts["identified_unique_contracts"])} · '
                   f'یافتهٔ تشخیصی: {shown(len(report["diagnostics"]))}</p>'
                   + reasons("کدهای تشخیصی ثبت", counts["diagnostics_by_reason"], {}))
    header = (f'<p><a href="/">بازگشت به مشاهدات آرشیوی</a></p>{COMBINED_WARNING}'
              f'<p>ثبت ثابت {raw(capture["capture_id"])} · دریافت HTTP در تهران (نه زمان بورس): '
              f'{display_receipt(capture["receipt_tehran"])}؛ زمان مظنه: {ltr("UNKNOWN")}</p>{totals}{diagnostics}'
              '<details><summary>منشأ، یکپارچگی و محدودیت‌ها</summary>' + fields([
                  *receipt_details(capture), ("ردهٔ داده", raw(capture["dataset_class"])),
                  ("نشانی منبع", raw(capture["source_url"])), ("وضعیت غربال ثبت", raw(capture["screening_status"])),
                  ("هش مهر", raw(capture["seal_sha256"])),
              ]) + "<ul>" + "".join(f"<li>{h(note)}</li>" for note in report["limitations_fa"]) + "</ul></details>")

    def link(**changes):
        return h(COMBINED_RESEARCH + "?" + urlencode(vars(query) | changes))

    tabs = '<nav class="pager" aria-label="بخش‌های پژوهش">' + "".join(
        f'<a href="{link(view=view, page=1)}" aria-current="{"page" if view == query.view else "false"}">{h(label)}</a>'
        for view, label in research.COMBINED_VIEWS.items()) + '</nav>'
    rows, total, number, pages = research.combined_rows(report, query)
    controls = ""
    if query.view == "options":
        controls = f'<form method="get" action="{COMBINED_RESEARCH}" class="filters">' + "".join(
            f"<label>{label}{field}</label>" for label, field in (
                ("جست‌وجوی نماد یا شناسهٔ اختیار یا پایه", f'<input name="q" maxlength="80" value="{h(query.q)}">'),
                ("نوع", select_input("kind", {"": "همه", "CALL": "CALL", "PUT": "PUT"}, query.kind)),
                ("وضعیت پژوهشی", select_input("status", {"": "همه", **research.COMBINED_STATUSES}, query.status)),
                ("غربال اولیه", select_input("screening", {"": "همه", **research.COMBINED_SCREENING}, query.screening)),
                ("نقدشوندگی ثبت‌شده", select_input("liquidity", {"": "همه", **research.COMBINED_LIQUIDITY}, query.liquidity)),
                ("سررسید دقیق (میلادی)", f'<input name="expiry" type="date" value="{h(query.expiry)}">'),
            )) + f'<div class="filter-actions"><button type="submit">اعمال فیلتر</button> <a href="{COMBINED_RESEARCH}">پاک‌کردن</a></div></form>'
        cards = "".join(combined_option_card(row) for row in rows)
    elif query.view == "underlyings":
        cards = "".join(combined_underlying_card(code, group) for code, group in rows)
    else:
        cards = "".join(combined_diagnostic_card(row) for row in rows)
    pager = '<nav class="pager" aria-label="صفحه‌بندی">' + "".join(
        f'<a href="{link(page=target)}">{label}</a>'
        for target, label in ((number - 1, "صفحهٔ قبل"), (number + 1, "صفحهٔ بعد"))
        if 1 <= target <= pages) + "</nav>"
    listing = f'<div class="candidate-grid">{cards}</div>' if cards else "<p>ردیفی برای نمایش نیست.</p>"
    return page("پژوهش آرشیوی کل بازار", header + tabs + '<section>'
                f'<h2>{h(research.COMBINED_VIEWS[query.view])}</h2>{controls}'
                f'<p>{shown(total)} نتیجه · صفحهٔ {shown(number)} از {shown(pages)}</p>{listing}{pager}</section>')


def home(store, observations, archive_failed, now, *, report=None, query=None):
    active, history = [], []
    current = store.current_fingerprints()
    for row in store.recent():
        state = visible_state(row, now)
        proposal = json.loads(row["payload"])
        item = (f'<li><a href="/review/{row["id"]}">{h(proposal["symbol"])} '
                f'{ltr(proposal["option_type"])}</a> — {h(STATES[state])}</li>')
        available = (state == "PENDING" and now >= timestamp(row["created_at"])
                     and current.get(row["proposal_key"]) == row["fingerprint"])
        (active if available else history).append(item)
    pending = ("<ul>" + "".join(active) + "</ul>") if active else (
        "<p>پیشنهاد واجد شرایط وجود ندارد. واچ‌لیست آرشیوی زمان معتبر مظنه ندارد؛ "
        "Greeks آزمایشی و عبور از غربال اولیه مجوز پیشنهاد نیستند. در این فاز منبع تأییدشده متصل نیست.</p>"
    )
    archived = []
    for d in observations:
        age = now - timestamp(d["receipt_utc"])
        age_note = ("ساعت دریافت جلوتر از ساعت دستگاه است؛ عمر نامعلوم."
                    if age.total_seconds() < 0 else
                     f"{shown(age.days)} روز و {shown(age.seconds // 3600)} ساعت و {shown((age.seconds // 60) % 60)} دقیقه")
        archived.append(f'<details><summary>{display_date(d["day"])} — {shown(d["candidate_count"])} نامزد</summary>' + fields([
            ("دریافت HTTP (تهران)", display_receipt(d["receipt_tehran"])),
            ("عمر دریافت در زمان نمایش (نه عمر مظنه)", age_note),
            ("تعداد ثبت‌های آن روز در آرشیو", shown(d["capture_count"])),
            ("قرارداد / دوطرفه از نظر ساختار", f'{shown(d["contract_count"])} / {shown(d["two_sided_count"])}'),
            ("نامزد غربال / واجد رتبه (نه پیشنهاد)", f'{shown(d["candidate_count"])} / {shown(d["rank_eligible_count"])}'),
        ]) + '<details><summary>فرادادهٔ فنی ثبت</summary><div class="record-details">' + fields(receipt_details(d))
            + "</div></details>" + reasons("اولین علت رد در نامزدسازی", d["candidate_rejections"], CANDIDATE_REASONS)
            + reasons("اولین علت رد در رتبه‌بندی", d["rank_rejections"], {}) + "</details>")
    if archive_failed:
        archive_note = "<p>خواندن یا اعتبارسنجی آرشیو ناموفق بود؛ دادهٔ ناقص نمایش داده نمی‌شود.</p>"
    elif not observations:
        archive_note = "<p>هیچ مشاهدهٔ آرشیوشده‌ای یافت نشد.</p>"
    else:
        archive_note = "<p>آخرین ثبت هر روز تهران (تا هفت روز مشاهده‌شده).</p>"
    cards = [("پیشنهاد تأییدشدهٔ معتبر در انتظار بررسی", len(active))]
    research_html = ""
    if report and observations:
        latest = report["days"][observations[0]["day"]]
        comparison = report["dashboard_comparison"]
        future_receipt = now < timestamp(latest["receipt_utc"])
        if future_receipt:
            comparison = None
        cards.extend([
            ("نامزد غربال اولیه", observations[0]["candidate_count"]),
            ("قراردادهای آخرین ثبت", latest["contract_count"]),
            ("دوطرفهٔ ساختاری", observations[0]["two_sided_count"]),
            ("عبور از قواعد رتبه‌بندی", latest["rank_eligible_count"]),
        ])
        clock_note = ("<p>زمان دریافت از ساعت دستگاه جلوتر است؛ مقایسه نمایش داده نمی‌شود.</p>"
                      if future_receipt else "")
        research_html = (f'<p>آخرین دریافت HTTP (تهران): {display_receipt(latest["receipt_tehran"])} (میلادی)</p>{clock_note}'
                         + candidate_section(latest, comparison, query or research.Query())
                         + comparison_section(comparison))
    return page("داشبورد تأیید انسانی", f"""<p><a href="{PREFLIGHT}">بررسی پیش از سفارش</a> — برآورد دستی، فقط‌خواندنی</p>
 <h2>مشاهدات آرشیوی TSE/IFB</h2>
 <p><a href="{COMBINED_RESEARCH}">پژوهش کل بازار برای ثبت آرشیوی {h(research.COMBINED_CAPTURE)}</a></p>
<p>دریافت HTTP از TSETMC زمان مظنهٔ بورس نیست. زمان و توالی دفتر سفارش نامعلوم‌اند.
این داده‌ها زنده، قابل اجرا یا مبنای پرشدن آزمایشی نیستند.</p>{card_grid(cards)}
{research_html}
<section><h2>پیشنهادهای در انتظار — <bdi dir="ltr">VERIFIED TRADE PROPOSAL</bdi></h2>
<p>این بخش از غربال آرشیوی مستقل است؛ تأیید فقط برای پژوهش است.</p>{pending}</section>
<section><details><summary>تاریخچهٔ درخواست‌ها</summary>
{'<ul>' + ''.join(history) + '</ul>' if history else 'درخواستی ثبت نشده است.'}</details></section>
<section><h2>تاریخچهٔ مشاهدات</h2>{archive_note}{''.join(archived)}</section>""")


def manual_number(form, key):
    value = form[key].translate(MANUAL_DIGITS)
    label = (PREFLIGHT_NUMBERS | PREFLIGHT_SIZING_NUMBERS)[key]
    zero_allowed = key in ("fee", "committed", "reserved", "cash", "max_loss", "fee_cap", "ask_depth")
    if not value:
        raise ValueError(f"{label}: وارد نشده است.")
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,17})", value) or (value == "0" and not zero_allowed):
        allowed = "صفر فقط اگر مقدار واقعاً صفر است" if zero_allowed else "بزرگ‌تر از صفر"
        raise ValueError(f"{label}: عدد صحیح و بدون جداکننده لازم است ({allowed}).")
    return int(value)


def manual_expiry(value):
    expiry_text = value.translate(MANUAL_DIGITS)
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expiry_text):
            raise ValueError
        return date.fromisoformat(expiry_text)
    except ValueError:
        raise ValueError("سررسید میلادی معتبر با قالب YYYY-MM-DD لازم است.") from None


def manual_settlement_date(value, label):
    """Validate Jalali shape and possible day ranges only; do not convert calendars."""
    if not value:
        return None
    value = value.translate(MANUAL_DIGITS)
    if re.fullmatch(r"[0-9]{4}/[0-9]{2}/[0-9]{2}", value):
        year, month, day = map(int, value.split("/"))
        if year > 0 and 1 <= month <= 12 and 1 <= day <= (31 if month <= 6 else 30):
            return value
    raise ValueError(f"{label}: قالب شمسی YYYY/MM/DD و شمارهٔ ماه و روز معتبر لازم است.")


def manual_contract(form):
    instrument = form["instrument"]
    code = instrument.translate(MANUAL_DIGITS)
    has_id = watch.positive_id(code)
    symbol = form["symbol"]
    if symbol and not MANUAL_SYMBOL.fullmatch(symbol.translate(MANUAL_DIGITS)):
        raise ValueError("توقف بررسی هویت: نماد قرارداد نامعتبر است.")
    if instrument and not has_id and (symbol or not MANUAL_SYMBOL.fullmatch(code)):
        raise ValueError("توقف بررسی هویت: شناسهٔ ابزار نامعتبر است؛ در کنار فیلد نماد فقط insCode وارد کنید.")
    if form["kind"] and form["kind"] not in ("CALL", "PUT"):
        raise ValueError("نوع قرارداد نامعتبر است؛ CALL یا PUT را انتخاب کنید.")
    observed_unit, unit_source = (form.get(key, "") for key in ("observed_strike_unit", "strike_unit_source"))
    if observed_unit and observed_unit not in PREFLIGHT_STRIKE_UNITS:
        raise ValueError("واحد مشاهده‌شدهٔ قیمت اعمال نامعتبر است.")
    if unit_source and unit_source not in PREFLIGHT_UNIT_SOURCES:
        raise ValueError("منشأ دستی واحد قیمت اعمال نامعتبر است.")
    # Blank fields remain unknown for comparison; the estimator still requires them.
    return dict(
        ins_code=code if has_id else None, symbol=symbol or (instrument if not has_id else None) or None,
        option_type=form["kind"] or None, maturity_date=manual_expiry(form["expiry"]) if form["expiry"] else None,
        strike_price=manual_number(form, "strike") if form["strike"] else None,
        strike_unit=form["strike_unit"] if form["strike_unit"] in PREFLIGHT_STRIKE_UNITS else None,
        contract_size=manual_number(form, "contract_size") if form["contract_size"] else None,
        observed_strike_unit=observed_unit or None, strike_unit_source=unit_source or None,
        **{key: manual_settlement_date(form.get(key, ""), label) for key, label in PREFLIGHT_SETTLEMENT_DATES.items()},
    )


def preflight_sizing(form, values, premium_per_contract, allocation_remaining, effective_loss_budget, days):
    """Size using a documented upper bound on acquisition charges, not the entered order's fee."""
    fee_cap = manual_number(form, "fee_cap") if form["fee_cap"] else None
    depth = manual_number(form, "ask_depth") if form["ask_depth"] else None
    source = form["fee_cap_source"]
    reference = form["fee_cap_ref"]
    checked = form["sizing_inputs_checked"]
    if source and source not in PREFLIGHT_FEE_SOURCES:
        raise ValueError("منشأ کران هزینهٔ خرید نامعتبر است.")
    if reference and not re.fullmatch(r"\w[\w ./:\u200c-]{0,79}", reference):
        raise ValueError("مرجع کران هزینهٔ خرید نامعتبر است؛ فقط شناسهٔ کوتاه و بدون اطلاعات حساب وارد کنید.")
    if checked not in ("", "yes"):
        raise ValueError("اظهار بررسی ورودی‌های سقف تعداد نامعتبر است.")
    reasons = []
    if not checked:
        reasons.append("بررسی واحد، ضریب، وجه نقد خالص و تعهدها توسط اپراتور اظهار نشده است.")
    if fee_cap is None or not source or not reference:
        reasons.append("کران هزینهٔ خرید یا منشأ مستند آن نامعلوم است؛ کارمزد کلِ تعداد واردشده قابل تعمیم نیست.")
    elif fee_cap * values["quantity"] < values["fee"]:
        reasons.append("کران هزینه با کارمزد اعلام‌شده برای تعداد واردشده تعارض دارد.")
    if days <= HOLD_DAYS:
        reasons.append(f"سررسید گذشته یا در افق {HOLD_DAYS} روز تقویمی است.")

    label = "حداکثر تعداد طبق محاسبهٔ ورودی‌ها"
    items = [("عمق فروش", shown(depth, suffix=" قرارداد") if depth is not None else
              f'{ltr("UNKNOWN")} — عمق در سقف محاسبه اعمال نشده است.')]
    if reasons:
        items += [(label, ltr("UNKNOWN")), ("علت نامعلوم بودن سقف", h(" ".join(reasons)))]
    else:
        cost = premium_per_contract + fee_cap
        requested_cost_bound = cost * values["quantity"]
        limits = {
            "ماندهٔ سقف تجمیعی سرمایه": max(0, allocation_remaining) // cost,
            "وجه نقد خالص": values["cash"] // cost,
            "بودجهٔ مؤثر زیان": effective_loss_budget // cost,
        }
        if depth is not None:
            limits["عمق فروش واردشده"] = depth
        maximum = min(limits.values())
        items += [
            ("کران هزینهٔ خرید هر قرارداد", shown(cost, suffix=" ریال به‌ازای هر قرارداد")),
            ("کران هزینهٔ خرید در معرض زیان برای تعداد واردشده", shown(requested_cost_bound, suffix=" ریال")),
            (label, shown(maximum, suffix=" قرارداد")),
            ("محدودیت تعیین‌کننده", h("، ".join(name for name, quantity in limits.items() if quantity == maximum))),
        ]
        if maximum == 0:
            items.append(("علت سقف صفر", "حداقل یک محدودیت اجازهٔ خرید یک قرارداد را نمی‌دهد."))
        if values["quantity"] > maximum:
            items.append(("هشدار تعداد", "تعداد واردشده از سقف محاسبه‌شده بیشتر است."))
        if requested_cost_bound > effective_loss_budget:
            items.append(("هشدار زیان", "کران هزینهٔ خرید برای تعداد واردشده از بودجهٔ مؤثر زیان بیشتر است."))
    return ('<section id="preflight-sizing"><h3>سقف تعداد — فقط پژوهشی، نه اندازهٔ پیشنهادی</h3>'
            + fields(items) + "</section>")


def preflight_checks(form, now, contract):
    """Pure arithmetic and warnings; no proposal or eligibility result."""
    instrument = contract["ins_code"] or contract["symbol"]
    if not instrument:
        raise ValueError("شناسهٔ ابزار یا نماد قرارداد: هیچ‌کدام وارد نشده‌اند.")
    if not contract["option_type"]:
        raise ValueError("نوع قرارداد: انتخاب نشده است؛ CALL یا PUT را انتخاب کنید.")
    expiry = contract["maturity_date"]
    if expiry is None:
        raise ValueError("تاریخ سررسید (میلادی): وارد نشده است.")
    for key, supported in (("strike_unit", PREFLIGHT_STRIKE_UNITS), ("price_unit", PREFLIGHT_PRICE_UNITS),
                           ("cash_unit", ("rial",))):
        if not form[key]:
            raise ValueError(f"{PREFLIGHT_UNIT_LABELS[key]}: انتخاب نشده است.")
        if form[key] not in supported:
            raise ValueError(f"{PREFLIGHT_UNIT_LABELS[key]}: نامعتبر یا مبهم است.")
    if contract["observed_strike_unit"] and not contract["strike_unit_source"]:
        raise ValueError("منشأ دستی واحد مشاهده‌شدهٔ اعمال: انتخاب نشده است.")
    if contract["strike_unit_source"] and not contract["observed_strike_unit"]:
        raise ValueError("واحد مشاهده‌شدهٔ قیمت اعمال: انتخاب نشده است.")
    values = {key: manual_number(form, key) for key in PREFLIGHT_NUMBERS}
    price_label, rial_factor, per_unit = PREFLIGHT_PRICE_UNITS[form["price_unit"]]
    principal = values["limit_price"] * rial_factor * values["quantity"]
    if per_unit:
        principal *= values["contract_size"]
    total = principal + values["fee"]
    cap = values["initial_equity"] * ALLOCATION_PERCENT // 100
    # All costs and manual budgets are in whole rials; floor the policy cap, never round up.
    policy_loss_cap = values["initial_equity"] * PREFLIGHT_LOSS_PERCENT // 100
    effective_loss_budget = min(values["max_loss"], policy_loss_cap)
    committed = values["committed"] + values["reserved"]
    days = (expiry - timestamp(now).astimezone(collector.TEHRAN).date()).days
    warnings = []
    if days < 0:
        warnings.append("تاریخ سررسید واردشده گذشته است.")
    elif days <= HOLD_DAYS:
        warnings.append(f"سررسید نزدیک است: حداکثر {HOLD_DAYS} روز تقویمی تا تاریخ واردشده. افق نگهداری قابل اتکا نیست.")
    if committed + principal > cap:
        warnings.append(f"جمع بهای خرید و تعهدهای واردشده از سقف {ALLOCATION_PERCENT}٪ سرمایهٔ اولیه بیشتر است.")
    elif committed + total > cap:
        warnings.append(f"با احتساب کارمزد، برآورد از سقف {ALLOCATION_PERCENT}٪ بیشتر است؛ هشدار احتیاطی.")
    if total > values["cash"]:
        warnings.append("برآورد با کارمزد از وجه نقد در دسترس واردشده بیشتر است.")
    if values["max_loss"] > policy_loss_cap:
        warnings.append(f"بودجهٔ زیان واردشده برای رعایت سقف {PREFLIGHT_LOSS_PERCENT}٪ سرمایهٔ اولیه کاهش یافت.")
    if total > effective_loss_budget:
        warnings.append("برآورد بهای خرید با کارمزد برای تعداد واردشده از بودجهٔ مؤثر زیان بیشتر است.")
    sizing = preflight_sizing(form, values, principal // values["quantity"], cap - committed,
                              effective_loss_budget, days)
    return fields([
        ("شناسه یا نماد / نوع", f"{ltr(instrument.translate(MANUAL_DIGITS))} / {ltr(form['kind'])}"),
        ("قیمت اعمال", shown(values["strike"], suffix=" " + PREFLIGHT_STRIKE_UNITS[form["strike_unit"]])),
        ("سررسید واردشده (میلادی) / فاصله از تاریخ تهران", f"{display_date(expiry.isoformat())} / {shown(days, suffix=' روز تقویمی')}"),
        ("ضریب (اندازهٔ قرارداد)", shown(values["contract_size"], suffix=" واحد پایه در هر قرارداد")),
        ("قیمت محدود خرید (پریمیوم واردشده)", shown(values["limit_price"], suffix=" " + price_label)),
        ("تعداد سفارش", shown(values["quantity"], suffix=" قرارداد")),
        ("بهای هر قرارداد، پیش از کارمزد", shown(principal // values["quantity"], suffix=" ریال به‌ازای هر قرارداد")),
        ("بهای خرید به قیمت محدود، پیش از کارمزد", shown(principal, suffix=" ریال")),
        ("کارمزد واردشده", shown(values["fee"], suffix=" ریال")),
        ("جمع برآورد خرید با کارمزد", shown(total, suffix=" ریال")),
        ("سرمایهٔ اولیهٔ واردشده", shown(values["initial_equity"], suffix=" ریال")),
        ("حداکثر زیان کل واردشده", shown(values["max_loss"], suffix=" ریال")),
        (f"سقف سیاست زیان ({PREFLIGHT_LOSS_PERCENT}٪ سرمایهٔ اولیه، به ریال کامل)",
         shown(policy_loss_cap, suffix=" ریال")),
        ("بودجهٔ مؤثر زیان", shown(effective_loss_budget, suffix=" ریال")),
        ("سقف تجمیعی سرمایه", shown(cap, suffix=" ریال")),
        ("بهای خرید موقعیت‌های باز", shown(values["committed"], suffix=" ریال")),
        ("رزروها و تعهدهای جداگانه", shown(values["reserved"], suffix=" ریال")),
        ("تعهدهای واردشده + بهای خرید (بدون کارمزد جدید)", shown(committed + principal, suffix=" ریال")),
        ("تعهدهای واردشده + بهای خرید با کارمزد (برآورد احتیاطی)", shown(committed + total, suffix=" ریال")),
        ("وجه نقد در دسترس واردشده", shown(values["cash"], suffix=" ریال")),
    ]) + sizing, warnings


def comparison_observation(cache, now):
    if cache is None:
        return None, "هیچ مشاهدهٔ آرشیوشده‌ای در دسترس نیست."
    report, failed = cache.get()
    if (failed or not report
            or (report.get("dataset_class"), report.get("source_url")) != (watch.DATA_CLASS, watch.SOURCE)):
        return None, "خواندن یا اعتبارسنجی آرشیو ناموفق بود؛ دادهٔ قدیمی جایگزین نمی‌شود."
    days = report["days"]
    if not days:
        return None, "هیچ مشاهدهٔ آرشیوشده‌ای در دسترس نیست."
    latest = days[max(days)]  # Validated receipts are ordered; one latest receipt per Tehran date.
    if timestamp(latest["receipt_utc"]) > timestamp(now):
        return None, "زمان دریافت آرشیو از ساعت دستگاه جلوتر است."
    return latest, ""


def contract_comparison(broker, latest, note):
    match, mismatch, unknown = "MATCHED_OBSERVATIONAL", "MISMATCH", "UNKNOWN"

    def compare(entered, archived):
        if entered is None or archived is None:
            return unknown
        return match if entered == archived else mismatch

    def symbol_key(symbol):
        # Broader lookup only to detect aliases/collisions; equality below is exact.
        return research.text_key(symbol).translate(MANUAL_DIGITS)

    def safe_symbol(value):
        return value if isinstance(value, str) and 0 < len(value) <= 80 and value.strip() and not any(
            unicodedata.category(c).startswith("C") and c != "\u200c" for c in value) else None

    def archived_int(key):
        value = archived.get(key)
        return value if type(value) is int and 0 < value <= MAX_SQLITE_INT else None

    code, symbol = broker["ins_code"], broker["symbol"]
    contracts = latest["contracts"] if latest else {}
    archive_code = code if code in contracts else None
    if not archive_code and symbol and latest:
        candidates = [key for key, row in contracts.items()
                      if safe_symbol(row.get("symbol")) and symbol_key(row["symbol"]) == symbol_key(symbol)]
        if len(candidates) == 1:
            archive_code = candidates[0]
            note = ("شناسهٔ واردشده در آخرین ثبت نیست؛ نماد به شناسهٔ دیگری اشاره می‌کند. "
                    "مشخصات آن شناسه مقایسه نمی‌شوند." if code else
                    "نماد در آخرین ثبت یکتاست، اما برای تطبیق هویت insCode را وارد کنید.")
        elif len(candidates) > 1:
            note = "نماد در آخرین ثبت مبهم است؛ هیچ قراردادی انتخاب نشد."
    if not archive_code and latest and not note:
        note = ("قرارداد در آخرین ثبت یافت نشد؛ دادهٔ قدیمی جایگزین نمی‌شود." if code or symbol
                else "شناسه یا نماد قرارداد وارد نشده است.")

    # Never use a symbol to substitute for a supplied but absent stable ID.
    archived = contracts.get(archive_code, {}) if code is None or archive_code == code else {}
    maturity = archived.get("maturity_date")
    try:
        expiry = datetime.strptime(maturity, "%Y%m%d").date() if (
            isinstance(maturity, str) and re.fullmatch(r"[0-9]{8}", maturity)) else None
    except ValueError:
        expiry = None
    kind = archived.get("option_type")
    kind = kind if kind in ("CALL", "PUT") else None
    strike, size = archived_int("strike_price"), archived_int("contract_size")
    # Current normalized archive rows have no strike unit. Only an explicit
    # supported per-underlying-unit declaration in the report permits comparison.
    unit = archived.get("strike_unit")
    unit = unit if isinstance(unit, str) and unit in PREFLIGHT_STRIKE_UNITS else None
    entered_unit, observed_unit, unit_source = (broker[k] for k in (
        "strike_unit", "observed_strike_unit", "strike_unit_source"))
    manual_conflict = bool(entered_unit and observed_unit and entered_unit != observed_unit)
    archive_conflict = bool(unit and any(value and value != unit for value in (entered_unit, observed_unit)))
    unit_conflict = manual_conflict or archive_conflict
    unit_status = (mismatch if unit_conflict else
                   unknown if bool(observed_unit) != bool(unit_source) else compare(entered_unit, unit))
    strike_status = compare(broker["strike_price"], strike) if unit_status == match else unknown

    def strike_text(value, unit):
        return f"{value} · {PREFLIGHT_STRIKE_UNITS.get(unit, 'واحد نامعلوم')}" if value is not None else None

    archive_symbol = safe_symbol(archived.get("symbol"))
    rows = [
        ("شناسهٔ ابزار (insCode)", code, archive_code, compare(code, archive_code)),
        ("نماد قرارداد", symbol, archive_symbol, compare(symbol, archive_symbol)),
        ("نوع اختیار", broker["option_type"], kind, compare(broker["option_type"], kind)),
        ("قیمت اعمال و واحد", strike_text(broker["strike_price"], entered_unit),
         strike_text(strike, unit), strike_status),
        ("سررسید (میلادی)", broker["maturity_date"], expiry, compare(broker["maturity_date"], expiry)),
        ("اندازهٔ قرارداد (واحد پایه در هر قرارداد)", broker["contract_size"], size,
         compare(broker["contract_size"], size)),
    ]
    observed_text = (
        f"انتخاب فرم: {PREFLIGHT_STRIKE_UNITS.get(entered_unit, 'نامعلوم')} / "
        f"مشاهده: {PREFLIGHT_STRIKE_UNITS.get(observed_unit, 'نامعلوم')} · "
        f"{PREFLIGHT_UNIT_SOURCES.get(unit_source, 'منشأ دستی نامعلوم')}"
    )
    rows.append(("واحد اعمال؛ مشاهده و منشأ دستی", observed_text, PREFLIGHT_STRIKE_UNITS.get(unit), unit_status))
    # No independently supported cash/physical settlement fields exist in this
    # archive schema. An expiry date or a lookalike field cannot substitute.
    rows.extend((f"{label} (شمسی)", broker[key], "در آرشیو نیست", unknown)
                for key, label in PREFLIGHT_SETTLEMENT_DATES.items())
    mismatches = [label for label, _, _, status in rows if status == mismatch]
    warning = ""
    if mismatches or rows[0][3] != match or rows[2][3] != match or (symbol and rows[1][3] == unknown):
        warning = ('<p class="identity-block"><strong>توقف بررسی: '
                   "هویت قرارداد نامعلوم است یا هویت/مشخصات اختلاف دارد.</strong> "
                   "پیش از هر اقدام، قرارداد و مشخصات را از مرجع رسمی بررسی کنید.</p>")
    notices = []
    if manual_conflict:
        notices.append("واحد مشاهده‌شدهٔ اعمال با واحد انتخاب‌شده در فرم تعارض دارد.")
    if archive_conflict:
        notices.append("واحد تصریح‌شدهٔ اعمال در آرشیو با اظهار دستی متفاوت است؛ مقدار قیمت مقایسه نشد.")
    if not unit:
        notices.append("واحد پشتیبانی‌شدهٔ قیمت اعمال در آرشیو نامعلوم است؛ مقدار خام مبنای ورود سفارش نیست.")
    if not entered_unit:
        notices.append("واحد انتخاب‌شدهٔ قیمت اعمال نامعلوم یا نامعتبر است.")
    if bool(observed_unit) != bool(unit_source):
        notices.append("شاهد دستی واحد اعمال یا طبقه‌بندی منشأ آن ناقص است.")
    if not code:
        notices.append("شناسهٔ ابزار وارد نشده است؛ بررسی شناسه کامل نیست.")
    if not symbol:
        notices.append("نماد وارد نشده است؛ بررسی نماد کامل نیست.")
    receipt = (f'<p>آخرین دریافت HTTP آرشیو TSETMC (تهران): {display_receipt(latest["receipt_tehran"])}.</p>'
               if latest else "")
    body = "".join(
        f'<tr><th scope="row">{h(label)}</th><td>{raw(entered)}</td><td>{raw(saved)}</td><td>{ltr(status)}</td></tr>'
        for label, entered, saved, status in rows
    )
    if note:
        notices.append(note)
    alerts = warning + ("<ul>" + "".join(f"<li>{h(message)}</li>" for message in notices) + "</ul>" if notices else "")
    return mismatches, alerts, (receipt +
            '<table class="contract-comparison"><thead><tr>'
            '<th scope="col">فیلد</th><th scope="col">ورودی دستی اپراتور</th><th scope="col">مشاهدهٔ تاریخی TSETMC</th>'
            f'<th scope="col">نتیجه</th></tr></thead><tbody>{body}</tbody></table>')


def preflight_page(token, *, estimate=None, warnings=(), invalid=False, alerts="", comparison=""):
    def input_field(name, label, size, mode="text"):
        return f'<label>{label}<input name="{name}" maxlength="{size}" inputmode="{mode}"></label>'

    def choice(name, label, options):
        return f'<label>{label}{select_input(name, {"": "نامشخص / انتخاب کنید", **options}, "")}</label>'

    controls = "".join((
        input_field("instrument", "شناسهٔ اختیار (insCode)؛ اگر معلوم نیست، نماد", 40),
        input_field("symbol", "نماد قرارداد (اختیاری در کنار insCode)", 40),
        choice("kind", "نوع قرارداد", {"CALL": "CALL", "PUT": "PUT"}),
        input_field("expiry", "تاریخ سررسید (میلادی، YYYY-MM-DD)", 10),
        choice("strike_unit", PREFLIGHT_UNIT_LABELS["strike_unit"], PREFLIGHT_STRIKE_UNITS),
        choice("observed_strike_unit", "واحد مشاهده‌شدهٔ قیمت اعمال (اختیاری)", PREFLIGHT_STRIKE_UNITS),
        choice("strike_unit_source", "منشأ دستی واحد مشاهده‌شده (اختیاری)", PREFLIGHT_UNIT_SOURCES),
        *(input_field(key, f"{label} از صفحهٔ ایزی‌تریدر (اختیاری، شمسی YYYY/MM/DD)", 10)
          for key, label in PREFLIGHT_SETTLEMENT_DATES.items()),
        choice("price_unit", PREFLIGHT_UNIT_LABELS["price_unit"], {k: v[0] for k, v in PREFLIGHT_PRICE_UNITS.items()}),
        choice("cash_unit", PREFLIGHT_UNIT_LABELS["cash_unit"], {"rial": "همهٔ مبالغ حساب، زیان و هزینه به ریال‌اند"}),
        *(input_field(name, label, 18, "numeric") for name, label in PREFLIGHT_NUMBERS.items()),
        *(input_field(name, label, 18, "numeric") for name, label in PREFLIGHT_SIZING_NUMBERS.items()),
        choice("fee_cap_source", "منشأ کران هزینهٔ خرید", PREFLIGHT_FEE_SOURCES),
        input_field("fee_cap_ref", "شناسه یا نسخهٔ سند کران هزینه (بدون اطلاعات حساب)", 80),
        '<label><input type="checkbox" name="sizing_inputs_checked" value="yes"> '
        'واحد پریمیوم، ضریب همان قرارداد، وجه نقد خالص و تعهدها را با منابع معتبر بررسی کرده‌ام.</label>',
    ))
    result = ""
    if invalid or estimate is not None:
        result = ('<section><h2>محاسبه نامعتبر است</h2>' if invalid else
                  '<section><h2>برآورد دستی — غیرقابل اجرا</h2>')
        if warnings:
            result += '<div class="preflight-warning"><ul role="alert">' + "".join(
                f"<li>{h(warning)}</li>" for warning in warnings) + "</ul></div>"
        result += estimate or ""
        result += "</section>"
    archive_warnings = (f'<section class="preflight-warning" role="alert"><h2>هشدارهای مقایسه با آرشیو</h2>'
                        f'{alerts}</section>' if alerts else "")
    return page("بررسی پیش از سفارش", f"""<p><a href="/">بازگشت به داشبورد</a></p>
<section><p><strong>فقط پژوهش — حتی تطابق همهٔ ورودی‌ها مجوز معاملهٔ واقعی نیست.</strong></p>
<p><strong>راهبرد اولیه: فقط خرید اختیار خرید یا اختیار فروش برای باز کردن موقعیت خرید.</strong>
این فرم فقط هزینهٔ خرید <bdi dir="ltr">BUY_TO_OPEN</bdi> برای <bdi dir="ltr">CALL</bdi> یا <bdi dir="ltr">PUT</bdi>
را برآورد می‌کند. خرید <bdi dir="ltr">PUT</bdi> یعنی موقعیت خرید اختیار فروش، نه فروش یا نوشتن آن.
فروش برای باز کردن موقعیت، موقعیت فروش بدون پوشش، راهبرد اعتباری و معامله با اعتبار مجاز نیستند.
فروش برای بستن موقعیت خرید موجود (<bdi dir="ltr">SELL_TO_CLOSE</bdi>) خروج است و در این فرم برآورد نمی‌شود.</p>
<p>فقط اعداد مشاهده‌شده در ایزی‌تریدر را دستی وارد کنید. دادهٔ دستی یا اسکرین‌شات مظنهٔ تأییدشده نیست.
زمان معتبر مظنه، منشأ مستقل و مشخصات رسمی قرارداد بررسی نمی‌شوند؛ این صفحه مجوز یا مسیر ارسال سفارش ندارد.</p>
<p>عدد صحیح و بدون جداکننده وارد کنید؛ واحد نامعلوم را حدس نزنید. رمز، اطلاعات حساب یا شناسهٔ سفارش وارد نکنید.
 کارمزد، مبلغ کل اعلام‌شده برای همین تعداد است، نه درصد یا کارمزد هر قرارداد؛ اگر نامعلوم است خالی بگذارید.
 مبالغ حساب، سقف زیان و هزینه‌ها را به ریال وارد کنید. بهای موقعیت‌های باز و رزروها را جدا وارد کنید.
 نقد باید پس از کسر رزروها باشد؛ فرم رزروها را فقط در سقف تجمیعی لحاظ می‌کند و دوباره از نقد کم نمی‌کند.
 موجودی و شواهد دستی مستقلاً توسط داشبورد تأیید نمی‌شوند.
 فقط برای پریمیومِ واردشده با واحد تومان، هر تومان برابر ۱۰ ریال است.
برای قیمتِ هر قرارداد، ضریب دوباره ضرب نمی‌شود؛ با این حال اندازهٔ قرارداد باید معلوم باشد.</p>
<p>حداکثر زیان کل را خودتان وارد کنید؛ پیش‌فرض ندارد. بودجهٔ مؤثر زیان هر معامله از مقدار واردشده و
سقف {PREFLIGHT_LOSS_PERCENT}٪ سرمایهٔ اولیه، هرکدام کمتر باشد، به دست می‌آید و از سقف تجمیعی خرید جداست.
برای سقف تعداد، کران هزینهٔ هر قرارداد باید
برای هر تعدادِ مورد بررسی همهٔ کارمزدها، حداقل کارمزد، گردکردن و سایر هزینه‌های الزامی خرید را پوشش دهد.
کارمزد یک سفارش یا تقسیم آن بر تعداد، به‌تنهایی شاهد چنین کرانی نیست. صفر فقط در صورت مستند بودن وارد شود.
عمق را فقط اگر برای همین شناسه، در مظنهٔ معتبر و تا قیمت محدود مستقلاً بررسی شده است وارد کنید؛ از آرشیو پر نمی‌شود.</p>
<p>کل هزینهٔ خرید در معرض زیان است؛ دستور حد ضرر تضمین خروج یا محدود کردن زیان نیست.
الزامات اضافهٔ اعمال، تسویه یا تحویل تا تأیید مستقل <bdi dir="ltr">UNKNOWN</bdi> هستند و در این محاسبه نیستند.</p>
<p>سقف تجمیعی {ALLOCATION_PERCENT}٪ سرمایهٔ اولیه و افق {HOLD_DAYS} روز تقویمی بررسی می‌شوند.
تاریخ سررسید ساعت توقف معامله نیست. محاسبه از تاریخ دستگاه در تهران است و برای اجرا معتبر نیست.</p></section>
 {result + archive_warnings if invalid else archive_warnings + result}
 <section class="preflight-checklist"><h2>چک‌لیست نهایی سفارش دستی — فقط در ایزی‌تریدر رسمی</h2>
 <ol>
 <li>شناسهٔ ابزار، نماد، CALL/PUT، قیمت اعمال و واحد آن، سررسید و ضریب را دوباره تطبیق دهید.</li>
 <li>بلافاصله پیش از هر ارسال دستی، نرخ زندهٔ خرید و فروش (bid/ask)، عمق، وضعیت جلسه و قیمت و واحد نمایش‌داده‌شده در فرم سفارش را بررسی کنید.</li>
 <li>تعداد قرارداد، ضریب، کارمزد کل، وجه کل لازم و تعهد سفارش‌های باز را با هم تطبیق دهید.</li>
 <li>وجه نقد در دسترس و سقف تجمیعی {ALLOCATION_PERCENT}٪ سرمایهٔ اولیه را دوباره بررسی کنید.</li>
 <li>پیش از تصمیم، حداکثر زیان ممکن (از جمله از دست رفتن کل پریمیوم و کارمزد خرید)، برنامهٔ خروج و مهلت آن را بررسی کنید؛ خروج تضمین نیست.</li>
 <li>سفارش‌های موجود، پرشدن‌های جزئی و لغوهای در انتظار را در ایزی‌تریدر بررسی کنید تا سفارش تکراری ندهید؛ تا روشن شدن وضعیت، دوباره ارسال نکنید.</li>
 </ol>
 <p><strong>اگر موردی بررسی نشده یا مبهم است، سفارش ندهید. حتی تکمیل چک‌لیست مجوز معامله نیست.</strong></p></section>
<section><h2>ورود دستی</h2>
<form method="post" action="{PREFLIGHT}" class="filters" autocomplete="off">
<input type="hidden" name="csrf" value="{h(token)}">
 {controls}<div class="filter-actions"><button type="submit">محاسبه و مقایسهٔ مشاهداتی</button>
 <a href="{PREFLIGHT}">پاک کردن</a></div></form></section>
 <section id="contract-comparison"><h2><bdi lang="en" dir="ltr">Manual EasyTrader vs archived contract comparison</bdi></h2>
  <p>مقایسهٔ دستی مشخصات قرارداد. زمان دریافت HTTP، زمان مظنهٔ بورس نیست. حتی تطابق همهٔ فیلدها
  تازگی مظنه، مشخصات رسمی یا مجوز معامله را ثابت نمی‌کند.</p>
  <p><strong>شاهد رسمی مشخصات، اعمال و تسویه: در دسترس نیست (UNKNOWN).</strong>
  تاریخ‌های تسویه فقط اظهار دستی‌اند؛ با سررسید میلادی مقایسه نمی‌شوند.
  کنترل تاریخ شمسی فقط قالب و دامنهٔ ممکن ماه/روز است؛ کبیسه و برابری تقویمی احراز نشده‌اند.</p>
 {comparison}
 <p><bdi dir="ltr">MATCHED_OBSERVATIONAL</bdi>: تطابق در آرشیو، بدون تأیید مستقل؛
  <bdi dir="ltr">MISMATCH</bdi>: اختلاف صریح؛ محاسبه متوقف می‌شود.
  <bdi dir="ltr">UNKNOWN</bdi>: داده، منشأ یا واحد کافی نیست.
 واحدهای متفاوت قیمت اعمال بدون مدرک تبدیل مقایسه نمی‌شوند؛ هیچ تبدیل ضمنی انجام نمی‌شود.</p></section>""")


def review(row, events, token, now, available):
    proposal = json.loads(row["payload"])
    quote, risk = proposal["quote"], proposal["risk"]
    state = visible_state(row, now)
    form = ""
    if state == "PENDING" and available and now >= timestamp(row["created_at"]):
        form = f"""<form method="post" action="/review/{row['id']}">
<input type="hidden" name="csrf" value="{h(token)}">
<input type="hidden" name="fingerprint" value="{h(row['fingerprint'])}">
<label><input type="checkbox" name="confirmed" value="yes" required>
جزئیات همین نسخه را بررسی کردم؛ تصمیم من فقط پژوهشی است.</label><br>
<button name="decision" value="REJECTED">رد</button>
<button name="decision" value="CANCELLED">لغو درخواست</button>
<button name="decision" value="APPROVED">تأیید پژوهشی</button></form>"""
    # No implicit/default approval: the server requires the named submit button.
    audit = "".join(f"<li>{ltr(e['at'])} — {h(STATES[e['state']])} — "
                    f"{'اپراتور محلی' if e['actor'] == 'operator' else 'سامانه'} — "
                    f"{h(EVENT_REASONS[e['reason']])}</li>" for e in events)
    details = fields([
        ("نماد / نوع", f"{h(proposal['symbol'])} / {ltr(proposal['option_type'])}"),
        ("شناسهٔ ابزار", ltr(proposal["ins_code"])),
        ("تعداد پیشنهادی / ضریب قرارداد", f"{proposal['quantity']:,} / {proposal['contract_multiplier']:,}"),
        ("پریمیوم هر واحد پایه (ریال)", f"{proposal['premium_rials_per_unit']:,}"),
        ("حداکثر بهای خرید (ریال، بدون هزینه‌های جانبی)", f"{proposal['maximum_purchase_cost_rials']:,}"),
        ("سقف تجمیعی از سرمایهٔ اولیه / نقد موجود / بهای باز (ریال)",
         f"{risk['allocation_limit_percent']}٪ ({risk['allocation_limit_rials']:,}) / "
         f"{risk['cash_rials']:,} / {risk['open_purchase_cost_rials']:,}"),
        ("حداکثر نگهداری از پرشدن، اگر روزی اجرا مجاز شود",
         f"{risk['max_holding_calendar_days']} روز تقویمی؛ حد زیان {risk['stop_loss_bps'] / 100:g}٪"),
        ("مهلت معاملهٔ قرارداد (UTC)", ltr(risk["contract_trade_cutoff_utc"])),
        ("منبع مظنه و مرجع شواهد", f"{ltr(quote['source'])} / {ltr(quote['evidence_ref'])}"),
        ("زمان مظنهٔ بورس / زمان دریافت (UTC)",
         f"{ltr(quote['exchange_quoted_at_utc'])} / {ltr(quote['received_at_utc'])}"),
        ("مهلت تصمیم (UTC)", ltr(quote["valid_until_utc"])),
    ])
    expiry_note = ("<p>مهلت گذشته است؛ ثبت انقضا در بازبینی بعدی انجام می‌شود.</p>"
                    if state != row["state"] else "")
    if state == "PENDING" and not available:
        expiry_note = "<p>نسخهٔ ذخیره‌شده دیگر واجد بررسی نیست و قابل تأیید نیست.</p>"
    return page("جزئیات درخواست", f'<p><a href="/">بازگشت به داشبورد</a></p><section>'
                f'<h2>{h(STATES[state])}</h2>{details}<details><summary>اثر انگشت نسخه</summary>'
                f'{ltr(row["fingerprint"])}</details>{expiry_note}{form}</section>'
                f'<section><h2>رویدادهای ممیزی (UTC)</h2><ol>{audit}</ol></section>')


class DashboardServer(HTTPServer):
    def server_bind(self):
        # HTTPServer's default implementation may do a reverse DNS lookup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def __init__(self, port, store, observations, *, archive_failed=False, clock=utc_now,
                 cache=None, combined_cache=None):
        self.store, self.observations, self.clock = store, observations, clock
        self.archive_failed = archive_failed
        self.cache = cache
        self.combined_cache = combined_cache
        self.csrf = secrets.token_hex(32)
        super().__init__((HOST, port), DashboardHandler)
        self.authority = f"{HOST}:{self.server_port}"
        self.origin = f"http://{self.authority}"


class DashboardHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_):
        pass

    def respond(self, status, body=b"", *, location=None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def fail(self, status, message):
        self.respond(status, page("درخواست انجام نشد", f'<p>{h(message)}</p><a href="/">بازگشت</a>'))

    def header_is(self, key, value):
        return self.headers.get_all(key) == [value]

    def read_form(self, keys, limit, *, optional=frozenset()):
        lengths = self.headers.get_all("Content-Length", [])
        if (not self.header_is("Content-Type", "application/x-www-form-urlencoded")
                or self.headers.get_all("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[1-9][0-9]{0,3}", lengths[0]) or int(lengths[0]) > limit):
            raise ValueError("invalid form framing")
        size = int(lengths[0])
        body = self.rfile.read(size)
        if len(body) != size or re.search(rb"%(?![0-9a-fA-F]{2})", body):
            raise ValueError("truncated or malformed form")
        allowed = keys | optional
        form = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                        max_num_fields=len(allowed), errors="strict")
        if not keys <= form.keys() <= allowed or any(len(v) != 1 for v in form.values()):
            raise ValueError("missing or duplicate form field")
        return {key: v[0] for key, v in form.items()}

    def valid_csrf(self, token):
        return re.fullmatch(r"[0-9a-f]{64}", token) and secrets.compare_digest(token, self.server.csrf)

    def do_GET(self):
        if not self.header_is("Host", self.server.authority):
            return self.fail(403, "میزبان محلی معتبر نیست.")
        try:
            path, _, raw = self.path.partition("?")
            if path == COMBINED_RESEARCH:
                if (self.headers.get_all("Sec-Fetch-Site") not in (None, ["same-origin"], ["none"])
                        or self.headers.get_all("Origin") not in (None, [self.server.origin])):
                    return self.fail(403, "مبدأ درخواست معتبر نیست.")
                try:
                    query = research.CombinedQuery.parse(raw)
                except ValueError:
                    return self.fail(400, "فیلتر یا پارامتر پژوهشی نامعتبر است.")
                cache = self.server.combined_cache
                report = cache.get() if cache is not None else None
                if report is None:
                    return self.respond(503, page("پژوهش آرشیوی کل بازار", COMBINED_WARNING +
                                                  "<p>گزارش ثبت در دسترس نیست. دادهٔ پیشین نمایش داده نمی‌شود.</p>"
                                                  '<p><a href="/">بازگشت به مشاهدات آرشیوی</a></p>'))
                return self.respond(200, combined_page(report, query))
            if path == "/":
                try:
                    query = research.Query.parse(raw)
                except ValueError:
                    return self.fail(400, "فیلتر یا پارامتر صفحه نامعتبر است.")
                observations, failed, report = self.server.observations, self.server.archive_failed, None
                if self.server.cache is not None:
                    report, failed = self.server.cache.get()
                    observations = summary_cards(report) if report else []
                return self.respond(200, home(self.server.store, observations, failed,
                                              self.server.clock(), report=report, query=query))
            if self.path == PREFLIGHT:
                return self.respond(200, preflight_page(self.server.csrf))
            if match := REVIEW.fullmatch(self.path):
                row, events = self.server.store.detail(match[1])
                if row:
                    current = self.server.store.current_fingerprints() if row["state"] == "PENDING" else {}
                    available = current.get(row["proposal_key"]) == row["fingerprint"]
                    return self.respond(200, review(row, events, self.server.csrf, self.server.clock(), available))
            self.fail(404, "صفحه یافت نشد.")
        except (sqlite3.Error, ValueError):
            self.fail(503, "خواندن داده‌های تأیید ممکن نیست.")

    def do_POST(self):
        if (not self.header_is("Host", self.server.authority)
                or not self.header_is("Origin", self.server.origin)
                or self.headers.get_all("Sec-Fetch-Site") not in (None, ["same-origin"])):
            return self.fail(403, "مبدأ درخواست معتبر نیست.")
        match = REVIEW.fullmatch(self.path)
        preflight = self.path == PREFLIGHT
        if not match and not preflight:
            return self.fail(404, "درخواست یافت نشد.")
        keys = {"csrf"} if preflight else {"csrf", "fingerprint", "decision", "confirmed"}
        try:
            form = self.read_form(keys, PREFLIGHT_MAX_FORM if preflight else MAX_FORM,
                                  optional=PREFLIGHT_FIELDS | PREFLIGHT_EVIDENCE_FIELDS if preflight else frozenset())
        except (ValueError, TimeoutError):
            return self.fail(400, "فرم نامعتبر یا ناقص است.")
        if not self.valid_csrf(form["csrf"]):
            return self.fail(403, "نشانهٔ امنیتی نامعتبر است؛ صفحه را تازه کنید.")
        if preflight:
            # Missing data fields get the same field-specific diagnostics as blank
            # inputs; framing, the allowlist and the CSRF field stay strict.
            form = dict.fromkeys(PREFLIGHT_FIELDS, "") | form
            try:
                contract = manual_contract(form)
            except ValueError as error:
                return self.respond(400, preflight_page(self.server.csrf, warnings=[str(error)], invalid=True))
            now = self.server.clock()
            latest, note = comparison_observation(self.server.cache, now)
            mismatches, alerts, comparison = contract_comparison(contract, latest, note)
            if mismatches:
                warning = "محاسبه متوقف شد؛ اختلاف صریح در: " + "، ".join(mismatches) + "."
                return self.respond(400, preflight_page(self.server.csrf, warnings=[warning], invalid=True,
                                                        alerts=alerts, comparison=comparison))
            try:
                estimate, warnings = preflight_checks(form, now, contract)
            except ValueError as error:
                return self.respond(400, preflight_page(self.server.csrf, warnings=[str(error)], invalid=True,
                                                        alerts=alerts, comparison=comparison))
            return self.respond(200, preflight_page(self.server.csrf, estimate=estimate, warnings=warnings,
                                                    alerts=alerts, comparison=comparison))
        fingerprint, decision = form["fingerprint"], form["decision"]
        if (form["confirmed"] != "yes" or decision not in DECISIONS
                or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
            return self.fail(400, "تصمیم و تأیید صریح همین نسخه لازم است.")
        try:
            result = self.server.store.decide(match[1], fingerprint, decision)
        except (sqlite3.Error, ValueError):
            return self.fail(503, "ثبت تصمیم ممکن نیست.")
        if result == "recorded":
            return self.respond(303, location=self.path)
        if result == "missing":
            return self.fail(404, "درخواست یافت نشد.")
        self.fail(409, "درخواست بسته، منقضی یا تغییر کرده است، یا ساعت دستگاه معتبر نیست؛ دوباره بررسی کنید.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="داشبورد آفلاین تأیید پژوهشی TSE/IFB")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.umask(0o077)
    try:
        store = ApprovalStore(DB, phase1_eligible)
        store.initialize()
        store.reconcile()
        with DashboardServer(args.port, store, [], cache=research.ArchiveCache(),
                             combined_cache=research.CombinedCache()) as server:
            print(f"داشبورد پژوهشی: {server.origin}/", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"داشبورد شروع نشد: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
