"""Reproducible offline audit of the last two Shadow Watch receipts."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
from statistics import median
import subprocess
import sys

import calculate_greeks as model
import contract_diagnostics as diagnostics
import greeks_evidence_registry as registry
import options_model_sensitivity as sensitivity
import shadow_collector as collector
import shadow_watch as watch
import validate_greeks as validator


TARGETS = {"60210342490439455": "ضخود1165", "13308854503358443": "ضخود1166"}
OUTPUT = watch.ROOT / "reports"
SCENARIOS = (
    ("baseline", "مبنای آزمایشی", model.RISK_FREE_RATE, model.DIVIDEND_YIELD),
    ("rate_low", "نرخ پایین فرضی", 0.30, 0.0),
    ("rate_if_effective", "اگر نرخ مبنا مؤثر سالانه باشد", math.log1p(model.RISK_FREE_RATE), 0.0),
    ("rate_high", "نرخ بالای فرضی", 0.50, 0.0),
    ("dividend", "بازده سود فرضی", model.RISK_FREE_RATE, 0.10),
    ("combined", "نرخ پایین و بازده سود فرضی", 0.30, 0.10),
)
EVIDENCE = {
    "risk_free_rate": {
        "status": "unverified", "annual_continuous": model.RISK_FREE_RATE,
        "finding_fa": "عدد ۴۳٫۹۴٪ در کد ثابت و با ترکیب پیوسته است؛ منبع، تاریخ، تعریف نرخ و منحنی تنزیل ریالی هم‌سررسید در دست نیست. تبدیل ln(1+0.4394) فقط اگر ۴۳٫۹۴٪ نرخ مؤثر سالانه باشد معنا دارد و نرخ مشاهده‌شده نیست.",
    },
    "dividend": {
        "status": "unverified", "annual_continuous": model.DIVIDEND_YIELD,
        "finding_fa": "q=0 اثباتِ نبود سود نیست. اطلاعیه‌های مؤثر سود نقدی و اقدامات شرکتی، زمان و مبلغ پرداخت و قواعد تعدیل اعمال/ضریب برای افق قرارداد لازم‌اند؛ سود گسسته با بازده پیوسته یکسان نیست.",
    },
    "day_count": {
        "status": "implemented_not_verified_as_market_convention", "denominator": model.DAYS_IN_YEAR,
        "finding_fa": "تقریب ACT/365 Fixed: اختلاف تاریخ میلادی دریافت تهران و endDate، تقسیم بر ۳۶۵؛ ساعت و کسر روز لحاظ نشده‌اند. شمارندهٔ فید جداست و در این گزارش جایگزین تاریخ نمی‌شود. مبنای روزشمار بازار یا منحنی و ساعت رسمی سررسید تأیید نشده‌اند.",
    },
    "model": {
        "status": "experimental_unverified", "name": "European Black–Scholes–Merton",
        "finding_fa": "مدل اروپاییِ بلک–شولز–مرتون با نوسان، نرخ و بازده پیوستهٔ ثابت، اعمال فقط در سررسید و پرداخت ساده است؛ محدودیت معامله/پوشش، اصطکاک، اعمال زودهنگام یا تأخیر مؤثر تسویه را مدل نمی‌کند. برازش به midpoint صحت مدل یا سبک اعمال بازار را ثابت نمی‌کند.",
    },
    "multiplier": {
        "status": "archive_field_not_authoritative",
        "finding_fa": "عدد contractSize از آرشیو خوانده می‌شود؛ واحد قیمت و پریمیوم، تعریف ضریب و تعدیل مؤثر آن تأیید نشده‌اند. مدل S/K/premium را هم‌واحد می‌گیرد و ضریب را اعمال نمی‌کند؛ اعداد ارزش کل قرارداد نیستند.",
    },
    "expiry": {
        "status": "archive_date_only",
        "finding_fa": "endDate و تاریخ نام بلند با سرنخ سری قابل تطبیق‌اند، نه با فهرست رسمی شناسه‌ها و اصلاحیه‌ها. آخرین لحظهٔ معامله، ساعت/منطقهٔ زمانی سررسید و مهلت اعمال از تاریخِ تنها نتیجه نمی‌شوند.",
    },
    "settlement": {
        "status": "secondary_lead_only",
        "finding_fa": "خلاصهٔ ثانویه از اعمال اروپایی و غیرخودکار، تسویهٔ نقدی توافقی و تحویل فیزیکی در سررسید می‌گوید. متن رسمی حاکم بر همین سری، پنجره/روش اعمال، پیش‌فرض عدم اقدام، قیمت مرجع، زمان و جریان پرداخت/تحویل و هزینه‌ها در دست نیست؛ نتیجه به کل TSE/IFB تعمیم ندارد.",
    },
    "quote_quality": {
        "status": "structural_only",
        "finding_fa": "زمان/توالی معتبر مظنهٔ اختیار، زمان معاملهٔ پایه، هم‌زمانی آن‌ها، وضعیت جلسه و عمق قابل اجرا احراز نشده‌اند. قیمت آخر پایه ورودی مدل است، نه قیمت پایانی؛ هر دو فقط مقادیر همان پاسخ‌اند. HTTP 200، دریافت و هش محلی تأیید مستقل نیستند.",
    },
}
SECONDARY_LEAD = {
    "url": "https://www.shakhesban.com/stocks/messages/280381",
    "notice_number_reported": "181/140511566", "notice_date_reported": "1405/08/04",
    "status": "local_summary_only_not_retrieved",
    "local_reference": "contract_diagnostics.py",
    "retrieved_at": None,
    "reported_jalali_expiry": diagnostics.JALALI,
    "reported_multiplier": diagnostics.SIZE,
    "reported_multiplier_unit": "shares",
}


def archive_pair(legacy, stream, through=None):
    records = [(f"legacy:{n}", path, meta) for _, n, path, meta in watch.records_in_order(legacy)]
    if stream.exists() or stream.is_symlink():
        with collector.locked(stream, exclusive=False):
            records.extend((f"manual:{m['sequence']}", m["path"], m)
                           for m in collector.read_stream(stream))
    seen, previous = set(), None
    for capture, path, meta in records:
        received = watch.parse_time(meta["receipt_utc"])
        manual = capture.startswith("manual:")
        watch.require(previous is None or (received > previous and
                      (not manual or watch.parse_time(meta["request_utc"]) > previous)),
                      "overlapping legacy/manual receipts")
        watch.require(not manual or meta["body_sha256"] not in seen, "manual body repeats earlier capture")
        if not manual:  # read_stream already verified the manual bodies under its lock.
            watch.read_object(path, watch.MAX_BODY, meta["body_sha256"])
        seen.add(meta["body_sha256"])
        previous = received
    if through is not None:
        ids = [record[0] for record in records]
        watch.require(through in ids, "unknown --through capture")
        records = records[:ids.index(through) + 1]
    watch.require(len(records) >= 2, "two observations required")
    return records[-2:]


def evaluate(row, receipt):
    """Research prices for all usable rows, irrespective of scanner gates."""
    expiry = datetime.strptime(row["maturity_date"], "%Y%m%d").date()
    remaining = (expiry - watch.parse_time(receipt).date()).days
    mid = model.quote_midpoint(row["best_bid"], row["best_ask"])
    spot, strike, kind = (row[k] for k in ("underlying_last_price", "strike_price", "option_type"))
    invalid = ("unusable_bid_ask" if mid is None else "missing_positive_spot" if not spot else
               "nonpositive_calendar_time" if remaining <= 0 else None)
    result = {"calendar_days": remaining, "midpoint": mid, "input_failure": invalid,
              "scenarios": [], "roundtrip": None, "bid_iv": None, "ask_iv": None}
    if invalid:
        return result
    args = kind, spot, strike, remaining / model.DAYS_IN_YEAR
    base_iv = None
    for key, _, rate, dividend in SCENARIOS:
        fitted = sensitivity.scenario(key, kind, spot=spot, strike=strike, days=remaining,
                                      rate=rate, dividend=dividend, premium=mid)
        inputs = (*args, rate, dividend)
        if key == "baseline":
            base_iv = fitted["iv"]
            for side in ("bid", "ask"):
                result[f"{side}_iv"] = model.implied_volatility(
                    kind, row[f"best_{side}"], *inputs[1:])
        result["scenarios"].append({
            "id": key, "iv": fitted["iv"], "iv_failure_reason": fitted["iv_failure_reason"],
            "delta": fitted["delta"], "theta_per_day": fitted["theta_per_day"],
            "refit_price": model.bs_price(*inputs, fitted["iv"]) if fitted["iv"] is not None else None,
            "fixed_baseline_iv_price": model.bs_price(*inputs, base_iv) if base_iv is not None else None,
        })
    if base_iv is not None:
        market, price, ok = validator.reconstruct({
            "underlying_model_price": spot, "strike_price": strike, "time_to_expiry_days": remaining,
            "iv_mid": round(base_iv * 100, model.IV_PERCENT_DECIMALS), "option_type": kind,
            "best_bid": str(row["best_bid"]), "best_ask": str(row["best_ask"]),
        })
        result["roundtrip"] = {"pass": ok, "absolute_error": abs(price - market)}
    return result


def summarize(states, evaluations):
    roundtrips = [ev["roundtrip"] for ev in evaluations.values() if ev["roundtrip"] is not None]
    market = {
        "contracts": len(states), "two_sided_price_and_depth": sum(r["two_sided"] for r in states.values()),
        "candidate_count": sum(r["candidate_eligible"] for r in states.values()),
        "rank_eligible_count": sum(r["rank_eligible"] for r in states.values()),
        "rank_first_rejections": dict(Counter(r["rank_rejection"] for r in states.values() if r["rank_rejection"])),
        "model_inputs_usable": sum(ev["input_failure"] is None for ev in evaluations.values()),
        "model_input_first_failures": dict(Counter(ev["input_failure"] for ev in evaluations.values()
                                                  if ev["input_failure"] is not None)),
        "roundtrips_checked": len(roundtrips),
        "roundtrips_skipped": len(states) - len(roundtrips),
        "roundtrips_failed": sum(not r["pass"] for r in roundtrips),
        "max_roundtrip_absolute_error": max((r["absolute_error"] for r in roundtrips), default=None),
    }
    scenario_stats = []
    for i, (key, *_) in enumerate(SCENARIOS):
        errors, changes = Counter(), defaultdict(list)
        solved = 0
        for ev in evaluations.values():
            if not ev["scenarios"]:
                continue
            base, alt = ev["scenarios"][0], ev["scenarios"][i]
            solved += alt["iv"] is not None
            if alt["iv_failure_reason"]:
                errors[alt["iv_failure_reason"]] += 1
            for field, scale in (("iv", 100), ("delta", 1), ("theta_per_day", 1)):
                if base[field] is not None and alt[field] is not None:
                    changes[field].append((alt[field] - base[field]) * scale)
            old_price, new_price = base["fixed_baseline_iv_price"], alt["fixed_baseline_iv_price"]
            if old_price and new_price is not None:
                changes["price_pct"].append(100 * (new_price / old_price - 1))
        scenario_stats.append({
            "id": key, "iv_solved": solved, "iv_failures": dict(errors),
            "paired_refits": len(changes["iv"]),
            "fixed_iv_prices_compared": len(changes["price_pct"]),
            "median_iv_change_pp": median(changes["iv"]) if changes["iv"] else None,
            "median_delta_change": median(changes["delta"]) if changes["delta"] else None,
            "median_theta_change_raw_per_day": median(changes["theta_per_day"]) if changes["theta_per_day"] else None,
            "median_fixed_iv_price_change_pct": median(changes["price_pct"]) if changes["price_pct"] else None,
        })
    market["scenarios"] = scenario_stats
    return market


def build_report(legacy=watch.DEFAULT_ARCHIVE, stream=collector.STREAM, through=None):
    observations, identities = [], {}
    for capture, path, meta in archive_pair(legacy, stream, through):
        states = watch.contract_states(path, meta)
        for code, row in states.items():
            identity = tuple(row[k] for k in watch.IDENTITY)
            watch.require(identities.setdefault(code, identity) == identity, f"identity changed: {code}")
        body, _ = watch.read_object(path, watch.MAX_BODY, meta["body_sha256"])
        raw_calls = {r.get("insCode_C"): r for r in body["instrumentOptMarketWatch"]}
        receipt = meta["receipt_tehran"]
        numbers = {code: evaluate(row, receipt) for code, row in states.items()}
        targets = {}
        for code, symbol in TARGETS.items():
            if code not in states:
                targets[code] = {"symbol": symbol, "status": "absent"}
                continue
            row, raw = states[code], raw_calls.get(code)
            watch.require(raw is not None and diagnostics.normalize(row["symbol"]).replace("ـ", "") == symbol,
                          f"unexpected target identity: {code}")
            match, issues = diagnostics.notice_side(raw, "C")
            targets[code] = {**row, "status": "observed", "evaluation": numbers[code],
                             "archive_long_name": raw.get("lVal30_C"),
                             "secondary_lead_match": match, "secondary_lead_issues": issues}
        observations.append({
            "capture_id": capture, "body_file": f"{path.parent.name}/{path.name}",
            **{k: meta[k] for k in ("receipt_tehran", "receipt_utc", "body_sha256",
                                   "exchange_quote_timestamp", "provider_quote_timestamp", "order_book_sequence")},
            "seal_sha256": meta.get("head_sha256"),
            "market_wide": summarize(states, numbers),
            "targets": targets,
        })
    files = (*watch.MODEL_FILES, "validate_greeks.py", "options_model_sensitivity.py", "contract_diagnostics.py",
             "shadow_watch.py", "shadow_collector.py", "greeks_evidence_registry.py", Path(__file__).name)
    return {
        "schema_version": 1, "dataset_class": watch.DATA_CLASS, "source_url": watch.SOURCE,
        "execution_eligible": False, "greeks_assumption_status": "experimental_unverified",
        "selection_basis": "last_two_http_receipts_not_session_closes",
        "daily_trade_activity_comparable": False, "counter_deltas_reported": False,
        "iv_units": "annual_fraction", "price_units": "raw_feed_units_not_scaled_by_multiplier",
        "theta_units": "raw_feed_units_per_calendar_day", "evidence": EVIDENCE, "secondary_lead": SECONDARY_LEAD,
        "evidence_registry": registry.current_assessment(),
        "scenarios": [{"id": k, "label_fa": label, "annual_continuous_rate": r, "annual_continuous_dividend": q}
                      for k, label, r, q in SCENARIOS],
        "code_sha256": {name: watch.digest((watch.ROOT / name).read_bytes()) for name in files},
        "observations": observations,
    }


def fmt(value, scale=1):
    return "—" if value is None else f"{value * scale:.6g}"


def counts(values):
    return "، ".join(f"`{k}`: {v}" for k, v in sorted(values.items())) or "—"


def markdown(report):
    latest = report["observations"][-1]["capture_id"]
    lines = [
        "# ممیزی آفلاین Greeks و مشخصات اختیار TSE/IFB", "",
        "**پژوهشی و غیرقابل اجرا — `experimental_unverified`.** دو دریافت آخر Shadow Watch، نه دو قیمت پایانی "
        "یا دو جلسهٔ تأییدشده. هیچ منبع تازه‌ای واکشی نشده است.", "",
        f"بازتولید از ریشهٔ مخزن: `python -B greeks_contract_report.py --through {latest}`. "
        "هش بدنه‌ها، مهرهای موجود و نسخهٔ کد در JSON ثبت شده‌اند؛ هش اصالت بازار را ثابت نمی‌کند.", "",
        "## ۱. کیفیت دادهٔ بازار", "",
        "| ثبت | زمان دریافت تهران (نه زمان مظنه) | SHA-256 بدنه | قرارداد / قیمت و عمق دوطرفه | نامزد / واجد رتبه |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for obs in report["observations"]:
        m = obs["market_wide"]
        lines.append(f"| {obs['capture_id']} | `{obs['receipt_tehran']}` | `{obs['body_sha256']}` | "
                     f"{m['contracts']} / {m['two_sided_price_and_depth']} | {m['candidate_count']} / {m['rank_eligible_count']} |")
    lines += ["", EVIDENCE["quote_quality"]["finding_fa"],
              "شمار و حجم تجمعی فقط مقدار ثبت‌شده در هر دریافت‌اند؛ ساعت‌های محلی متفاوت‌اند و تفاضل شمارنده‌ها "
              "فعالیت روزانه، سرعت معامله یا حجم بین دو ثبت نیست. اختلاف مظنه بازده قابل اجرا نیست.", "",
              "| ثبت / نماد | آخر / پایانی پایه | bid / ask (وسط) | اسپرد٪ | عمق bid / ask | شمار / حجم تجمعی | روز مدل / فید | اولین رد |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for obs in report["observations"]:
        for row in obs["targets"].values():
            label = f"{obs['capture_id']} / {row['symbol']}"
            if row["status"] == "absent":
                lines.append(f"| {label} | غایب | — | — | — | — | — | — |")
                continue
            ev = row["evaluation"]
            lines.append(f"| {label} | {row['underlying_last_price']} / {row['underlying_closing_price']} | "
                         f"{row['best_bid']} / {row['best_ask']} ({fmt(ev['midpoint'])}) | {fmt(row['spread_percent'])} | "
                         f"{row['bid_volume']} / {row['ask_volume']} | {row['trade_count']} / {row['trade_volume']} | "
                         f"{ev['calendar_days']} / {row['remaining_days']} | "
                         f"{row['candidate_rejection'] or row['rank_rejection'] or '—'} |")
    old, new = (obs["targets"] for obs in report["observations"])
    lines.append("")
    for code in TARGETS:
        a, b = old[code], new[code]
        if a["status"] == b["status"] == "observed":
            lines.append(f"- {b['symbol']}: وسط از {fmt(a['evaluation']['midpoint'])} به {fmt(b['evaluation']['midpoint'])} "
                         f"و اسپرد از {fmt(a['spread_percent'])}٪ به {fmt(b['spread_percent'])}٪ رسید (مشاهده‌ای).")
    lines += ["", "## ۲. صحت عددی، نه اعتبار بازار", "",
              "دامنه: همهٔ شناسه‌های هر پاسخ، نه فقط نامزدهای اسکنر؛ ورودی مدل به bid/ask مثبت و مرتب، "
              "قیمت آخر مثبت پایه و زمان مثبت نیاز دارد، نه عمق. بازسازی با `validate_greeks.reconstruct` "
              "از IV درصدی گرد‌شده تا هشت رقم و با **همان مدل** انجام شد. آزمون‌های مرجع و تفاضل مرکزی در "
              "`test_greeks_audit.py` مکمل‌اند؛ نتیجهٔ بازسازی صحت داده یا تناسب مدل را تأیید نمی‌کند. "
              "شکست کران تحت فرض مدل، به‌تنهایی نشانهٔ آربیتراژ یا خرابی فید نیست.", ""]
    for obs in report["observations"]:
        m = obs["market_wide"]
        lines += [f"- **{obs['capture_id']}**: {m['model_inputs_usable']} ورودی قابل محاسبه؛ بازسازی "
                  f"{m['roundtrips_checked']}، خارج از تلورانس {m['roundtrips_failed']}، بررسی‌نشده "
                  f"{m['roundtrips_skipped']}؛ بیشینهٔ خطای مطلق {fmt(m['max_roundtrip_absolute_error'])} در واحد خام.",
                  f"  - ورودی خارج از دامنه (اولین علت): {counts(m['model_input_first_failures'])}؛ "
                  f"شکست IV مبنا: {counts(m['scenarios'][0]['iv_failures'])}؛ "
                  f"اولین رد رتبهٔ نامزدها: {counts(m['rank_first_rejections'])}."]
        for row in obs["targets"].values():
            if row["status"] == "observed":
                ev = row["evaluation"]
                base = ev["scenarios"][0] if ev["scenarios"] else {}
                lines.append(f"  - {row['symbol']}: IV مبنای bid / وسط / ask (%) = "
                             f"{fmt(ev['bid_iv'], 100)} / {fmt(base.get('iv'), 100)} / {fmt(ev['ask_iv'], 100)}؛ "
                             f"خطای بازسازی وسط: {fmt((ev['roundtrip'] or {}).get('absolute_error'))}.")
    lines += ["", "## ۳. شواهد مشخصات قرارداد", "",
              f"رجیستری آفلاین نسخهٔ `{report['evidence_registry']['registry_version']}`؛ "
              "شکاف‌ها در [گزارش شواهد](greeks-evidence-gaps-v1.fa.md) و هش رجیستری در JSON. "
              "رجیستری هیچ ورودی مدل یا صلاحیت اجرا را تغییر نمی‌دهد.", "",
              "سرنخ: بازنشر شاکهبان، اطلاعیهٔ منتسب `181/140511566` مورخ `1405/08/04`، فقط از خلاصهٔ محلی "
              "`contract_diagnostics.py`؛ در این خلاصه اندازهٔ قرارداد ۱٬۰۰۰ سهم ذکر شده است. پیوند در JSON؛ "
              "سند و زمان دریافت مستقل در دست نیست. تطبیق خانوادگی "
              "تأیید فهرست رسمی قراردادها نیست.", "",
              "| ثبت / نماد | شناسه اختیار / پایه | نوع / اعمال | endDate / نام بلند آرشیو | ضریب خام | تطبیق با سرنخ |",
              "| --- | --- | --- | --- | ---: | --- |"]
    for obs in report["observations"]:
        for code, row in obs["targets"].items():
            if row["status"] == "observed":
                match = " ".join([f"`{row['secondary_lead_match']}`", *row["secondary_lead_issues"]])
                lines.append(f"| {obs['capture_id']} / {row['symbol']} | `{code}` / `{row['underlying_code']}` | "
                             f"{row['option_type']} / {row['strike_price']} | `{row['maturity_date']}` / {row['archive_long_name']} | "
                             f"{row['contract_size']} | {match} |")
    lines += [""] + [f"- **{label}:** {EVIDENCE[key]['finding_fa']}" for key, label in
                     (("multiplier", "ضریب"), ("expiry", "سررسید"), ("settlement", "اعمال و تسویه"))]
    lines += ["", "## ۴. فرض‌های قیمت‌گذاری و حساسیت", ""] + [
        f"- **{label}:** {EVIDENCE[key]['finding_fa']}" for key, label in
        (("risk_free_rate", "نرخ"), ("dividend", "بازده سود"), ("day_count", "زمان"), ("model", "مدل"))]
    lines += ["", "سناریوها فرضی‌اند. در ستون‌های IV، دلتا و تتا، پریمیوم وسط ثابت و IV دوباره برازش می‌شود؛ "
              "قیمتِ برازش‌شده به تعریف باید تقریباً همان وسط باشد. در ستون قیمت مدل، IV مبنای **همان ردیف** ثابت است "
              "تا اثر تغییر نرخ/سود دیده شود؛ حتی اگر سناریوی تازه IV ندهد. تتا در واحد خام به ازای روز تقویمی است.", "",
              "| ثبت / نماد | سناریو | r٪ / q٪ پیوسته | IV٪ | دلتا | تتا / روز | قیمت با IV ثابت | شکست IV |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    for obs in report["observations"]:
        for row in obs["targets"].values():
            for s, (_, label, r, q) in zip(row.get("evaluation", {}).get("scenarios", []), SCENARIOS):
                lines.append(f"| {obs['capture_id']} / {row['symbol']} | {label} | {fmt(r, 100)} / {fmt(q, 100)} | "
                             f"{fmt(s['iv'], 100)} | {fmt(s['delta'])} | {fmt(s['theta_per_day'])} | "
                             f"{fmt(s['fixed_baseline_iv_price'])} | {s['iv_failure_reason'] or '—'} |")
    lines.append("")
    for row in new.values():
        solved = [s for s in row.get("evaluation", {}).get("scenarios", []) if s["iv"] is not None]
        if solved:
            lo, hi = min(s["iv"] for s in solved), max(s["iv"] for s in solved)
            lines.append(f"- در {latest}، دامنهٔ IV در سناریوهای حل‌شدهٔ {row['symbol']}: "
                         f"{fmt(lo, 100)}٪ تا {fmt(hi, 100)}٪؛ این بازه عدم‌قطعیت آماری نیست.")
    lines += ["", "### حساسیت کل واچ‌لیست، جداگانه برای هر دریافت", "",
              "میانهٔ تغییر نسبت به مبنای **همان دریافت**؛ IV/دلتا/تتا فقط روی قراردادهای دارای جواب در هر دو مدل. "
              "تغییر نسبی قیمت روی قراردادهای دارای IV مبنا محاسبه می‌شود و به جواب IV جدید وابسته نیست. "
              "CALL و PUT در این میانه‌ها تجمیع شده‌اند؛ میانهٔ تتا در واحد خام است و واحد فید را تأیید نمی‌کند.", "",
              "| ثبت / سناریو | IV حل‌شده / جفت معتبر | تعداد قیمت ثابت | Δ IV (واحد درصد) | Δ دلتا | Δ تتا / روز | Δ قیمت٪ |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for obs in report["observations"]:
        for s, (_, label, _, _) in zip(obs["market_wide"]["scenarios"], SCENARIOS):
            lines.append(f"| {obs['capture_id']} / {label} | {s['iv_solved']} / {s['paired_refits']} | "
                         f"{s['fixed_iv_prices_compared']} | {fmt(s['median_iv_change_pp'])} | "
                         f"{fmt(s['median_delta_change'])} | {fmt(s['median_theta_change_raw_per_day'])} | "
                         f"{fmt(s['median_fixed_iv_price_change_pct'])} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path, default=watch.DEFAULT_ARCHIVE)
    parser.add_argument("--stream", type=Path, default=collector.STREAM)
    parser.add_argument("--through", help="end at this capture ID instead of the archive tip")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.legacy, args.stream, args.through)
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        text = markdown(report)
        OUTPUT.mkdir(exist_ok=True)
        (OUTPUT / "greeks-contract-validation.json").write_text(encoded, encoding="utf-8")
        (OUTPUT / "greeks-contract-validation.fa.md").write_text(text, encoding="utf-8")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Offline report stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
