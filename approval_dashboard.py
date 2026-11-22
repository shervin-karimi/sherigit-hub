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
from urllib.parse import parse_qs, urlencode

from approval_workflow import ApprovalStore, DECISIONS, utc_now
import dashboard_research as research
from paper_quotes import timestamp
import shadow_collector as collector
import shadow_multiday
import shadow_watch as watch


HOST = "127.0.0.1"
DB = watch.ROOT / "data/approval-dashboard/approvals.sqlite3"
REVIEW = re.compile(r"/review/([0-9a-f]{32})")
MAX_FORM = 1024
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
    return page("داشبورد تأیید انسانی", f"""<h2>مشاهدات آرشیوی TSE/IFB</h2>
<p>دریافت HTTP از TSETMC زمان مظنهٔ بورس نیست. زمان و توالی دفتر سفارش نامعلوم‌اند.
این داده‌ها زنده، قابل اجرا یا مبنای پرشدن آزمایشی نیستند.</p>{card_grid(cards)}
{research_html}
<section><h2>پیشنهادهای در انتظار — <bdi dir="ltr">VERIFIED TRADE PROPOSAL</bdi></h2>
<p>این بخش از غربال آرشیوی مستقل است؛ تأیید فقط برای پژوهش است.</p>{pending}</section>
<section><details><summary>تاریخچهٔ درخواست‌ها</summary>
{'<ul>' + ''.join(history) + '</ul>' if history else 'درخواستی ثبت نشده است.'}</details></section>
<section><h2>تاریخچهٔ مشاهدات</h2>{archive_note}{''.join(archived)}</section>""")


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

    def __init__(self, port, store, observations, *, archive_failed=False, clock=utc_now, cache=None):
        self.store, self.observations, self.clock = store, observations, clock
        self.archive_failed = archive_failed
        self.cache = cache
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
        self.send_header("Referrer-Policy", "no-referrer")
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

    def do_GET(self):
        if not self.header_is("Host", self.server.authority):
            return self.fail(403, "میزبان محلی معتبر نیست.")
        try:
            path, _, raw = self.path.partition("?")
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
        if not match:
            return self.fail(404, "درخواست یافت نشد.")
        lengths = self.headers.get_all("Content-Length", [])
        if (not self.header_is("Content-Type", "application/x-www-form-urlencoded")
                or self.headers.get_all("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[1-9][0-9]{0,3}", lengths[0]) or int(lengths[0]) > MAX_FORM):
            return self.fail(400, "قالب فرم معتبر نیست.")
        try:
            size = int(lengths[0])
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("truncated request")
            form = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                            max_num_fields=4, errors="strict")
            if (set(form) != {"csrf", "fingerprint", "decision", "confirmed"}
                    or any(len(v) != 1 for v in form.values())):
                raise ValueError("missing or duplicate form field")
        except (ValueError, TimeoutError):
            return self.fail(400, "فرم نامعتبر یا ناقص است.")
        token, fingerprint, decision = (form[k][0] for k in ("csrf", "fingerprint", "decision"))
        if not re.fullmatch(r"[0-9a-f]{64}", token) or not secrets.compare_digest(token, self.server.csrf):
            return self.fail(403, "نشانهٔ امنیتی نامعتبر است؛ صفحه را تازه کنید.")
        if (form["confirmed"] != ["yes"] or decision not in DECISIONS
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
        with DashboardServer(args.port, store, [], cache=research.ArchiveCache()) as server:
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
