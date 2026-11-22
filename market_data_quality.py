"""Audit two pinned, non-executable TSETMC observations without changing the archive."""

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path
import re
from statistics import median
import subprocess
import sys

from candidate_rules import MAX_SPREAD, MIN_TRADES
import greeks_evidence_registry as evidence
from rank_candidates import MAX_SPREAD as RANK_SPREAD
import shadow_collector as collector
import shadow_watch as watch


CAPTURES = ((1, "2026-11-21"), (2, "2026-11-22"))
KHODRO = "65883838195688438"
SERIES_EXPIRY = "20270127"
TARGETS = {"60210342490439455": "ضـخود1165", "13308854503358443": "ضـخود1166"}
SIDE_FIELDS = {
    "best_bid": "pMeDem", "best_ask": "pMeOf", "bid_volume": "qTitMeDem",
    "ask_volume": "qTitMeOf", "trade_count": "zTotTran", "trade_volume": "qTotTran5J",
}
BASE_FIELDS = {"underlying_last_price": "pDrCotVal_UA", "underlying_closing_price": "pClosing_UA",
               "remaining_days": "remainedDay"}
QUOTE_CLASSES = ("two_sided", "bid_only", "ask_only", "missing", "crossed", "inconsistent")
TIME_FIELDS = ("exchange_quote_timestamp", "provider_quote_timestamp", "order_book_sequence")
NO_MODEL = "فرض‌های Greeks هنوز تأیید نشده‌اند"


def number(value, *, signed=False):
    if value is None:
        return None, "missing"
    if type(value) is not int or not (-2**63 if signed else 0) <= value <= 2**63 - 1:
        return None, "invalid"
    return value, "zero" if value == 0 else "present"


def inspect_contract(raw, side):
    """Inspect raw price/depth/spot/counter fields without relying on scanner verdicts."""
    fields = {key: raw.get(f"{source}_{side}") for key, source in SIDE_FIELDS.items()}
    fields.update((key, raw.get(source)) for key, source in BASE_FIELDS.items())
    observed, status = {}, {}
    for key, value in fields.items():
        observed[key], status[key] = number(value, signed=key == "remaining_days")

    bid, ask, bd, ad, trades, volume = (observed[key] or 0 for key in SIDE_FIELDS)
    ordered = bid > 0 and ask >= bid
    spread = (ask - bid) / ((ask + bid) / 2) * 100 if ordered else None
    mismatched = [name for name, price, depth in (("bid", bid, bd), ("ask", ask, ad))
                  if bool(price) != bool(depth)]
    bad_quote_fields = [key for key in ("best_bid", "best_ask", "bid_volume", "ask_volume")
                        if status[key] == "invalid"]
    if bid > ask > 0:
        quote = "crossed"
    elif bad_quote_fields or mismatched:
        quote = "inconsistent"
    else:
        quote = "two_sided" if ordered else "bid_only" if bid else "ask_only" if ask else "missing"
    liquidity = []
    if not bd:
        liquidity.append("no_positive_bid_depth")
    if not ad:
        liquidity.append("no_positive_ask_depth")
    if trades < MIN_TRADES:
        liquidity.append("trade_count_below_minimum_or_unusable")
    if not volume:
        liquidity.append("no_positive_trade_volume")
    bases = ("underlying_last_price", "underlying_closing_price")
    return observed | {
        "field_status": status, "quote_class": quote, "locked_prices": ordered and bid == ask,
        "spread_percent": round(spread, 6) if spread is not None else None,
        "spread_above_screening_limit": spread is not None and spread > MAX_SPREAD,
        "price_depth_mismatch_sides": mismatched, "liquidity_issues": liquidity,
        "trade_counter_inconsistent": all(status[key] in ("present", "zero")
                                           for key in ("trade_count", "trade_volume"))
                                      and bool(trades) != bool(volume),
        "underlying_reference_status": ("present_unverified_synchrony_unknown"
                                        if all(status[k] == "present" for k in bases)
                                        else "missing_or_invalid_reference"),
    }


def raw_contracts(body):
    contracts = {}
    for raw in body:
        for side, kind in (("C", "CALL"), ("P", "PUT")):
            code = raw.get(f"insCode_{side}")
            if code is None or code in ("", "0") or (type(code) is int and code == 0):
                continue
            base, strike, expiry, size = (raw.get(k) for k in ("uaInsCode", "strikePrice", "endDate", "contractSize"))
            watch.require(watch.positive_id(code) and watch.positive_id(base), "invalid option or underlying ID")
            watch.require(code not in contracts, f"duplicate instrument ID: {code}")
            watch.require(all(number(v)[1] == "present" for v in (strike, size)), f"invalid strike/size: {code}")
            try:
                watch.require(isinstance(expiry, str) and re.fullmatch(r"[0-9]{8}", expiry), "invalid expiry")
                date.fromisoformat(f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}")
            except ValueError as error:
                raise watch.ArchiveError(f"invalid expiry: {code}") from error
            symbol, base_symbol = raw.get(f"lVal18AFC_{side}"), raw.get("lval30_UA")
            watch.require(all(v is None or isinstance(v, str) for v in (symbol, base_symbol)), f"invalid symbol: {code}")
            contracts[code] = {
                "option_type": kind, "underlying_code": base, "strike_price": strike,
                "maturity_date": expiry, "contract_size": size, "symbol": symbol,
                "underlying_symbol": base_symbol, **inspect_contract(raw, side),
            }
    watch.require(contracts, "no identifiable option contracts")
    return contracts


def identity(row):
    return tuple(row[key] for key in watch.IDENTITY)


def summarize(rows, screening_available):
    rows = list(rows)
    quote = Counter(r["quote_class"] for r in rows)
    spreads = [r["spread_percent"] for r in rows if r["spread_percent"] is not None]
    candidates = [r["screening"] for r in rows if r["screening"] and r["screening"]["candidate_eligible"]]
    return {
        "contracts": len(rows), "quote_classes": {key: quote[key] for key in QUOTE_CLASSES},
        "ordered_positive_prices": len(spreads), "locked_prices": sum(r["locked_prices"] for r in rows),
        "spread_above_screening_limit": sum(r["spread_above_screening_limit"] for r in rows),
        "median_ordered_price_spread_percent": round(median(spreads), 6) if spreads else None,
        "insufficient_liquidity": sum(bool(r["liquidity_issues"]) for r in rows),
        "liquidity_issue_counts": dict(Counter(issue for r in rows for issue in r["liquidity_issues"])),
        "field_status_counts": {
            key: dict(Counter(r["field_status"][key] for r in rows)) for key in (*SIDE_FIELDS, *BASE_FIELDS)
        },
        "trade_counter_inconsistent": sum(r["trade_counter_inconsistent"] for r in rows),
        "missing_or_invalid_underlying_reference": sum(
            r["underlying_reference_status"] == "missing_or_invalid_reference" for r in rows),
        "underlying_reference_present_but_unverified": sum(
            r["underlying_reference_status"] == "present_unverified_synchrony_unknown" for r in rows),
        "missing_exchange_quote_timestamp": len(rows), "missing_provider_quote_timestamp": len(rows),
        "missing_order_book_sequence": len(rows), "quote_provenance_blocked": len(rows),
        "screening": None if not screening_available else {
            "passes_existing_screen": len(candidates),
            "candidate_quote_provenance_blocked": len(candidates),
            "candidate_model_unverified": sum(r["greeks_assumption_status"] == "experimental_unverified" for r in candidates),
            "first_rank_rejection_model_unverified": sum(r["rank_rejection"] == NO_MODEL for r in candidates),
            "rank_eligible": sum(r["rank_eligible"] for r in candidates),
            "screen_first_rejections": dict(Counter(r["screening"]["candidate_rejection"]
                                                   for r in rows if not r["screening"]["candidate_eligible"])),
            "rank_first_rejections": dict(Counter(r["rank_rejection"] for r in candidates if r["rank_rejection"])),
        },
    }


def read_capture(meta):
    path = meta["path"]
    body, raw = watch.read_object(path, watch.MAX_BODY, meta["body_sha256"])
    body = body.get("instrumentOptMarketWatch")
    watch.require(isinstance(body, list) and type(meta["rows"]) is int and len(body) == meta["rows"]
                  and type(meta["byte_count"]) is int and len(raw) == meta["byte_count"] and body
                  and all(isinstance(r, dict) for r in body), "body does not match receipt")
    rows = raw_contracts(body)
    # The existing replay rejects malformed numeric values at snapshot level. Keep the
    # raw diagnostics, but don't invent a screening/ranking result for this capture.
    available = not any("invalid" in row["field_status"].values() for row in rows.values())
    states = watch.contract_states(path, meta) if available else None
    if states is not None:
        watch.require(states.keys() == rows.keys() and all(identity(states[k]) == identity(rows[k]) for k in rows),
                      "raw and scanner instrument identities disagree")
    bases = defaultdict(list)
    for code, row in rows.items():
        row["screening"] = ({key: states[code][key] for key in
                             (*watch.ELIGIBILITY_FIELDS, "iv_status", "greeks_assumption_status")}
                            if states is not None else None)
        bases[row["underlying_code"]].append(row)

    groups = {}
    for code, members in sorted(bases.items()):
        groups[code] = {
            "symbols": sorted({r["underlying_symbol"] for r in members if r["underlying_symbol"]}),
            "reference_values": {
                k: sorted({r[k] for r in members if r["field_status"][k] == "present"})
                for k in ("underlying_last_price", "underlying_closing_price")
            },
            **summarize(members, available),
        }
    series = [r for r in bases.get(KHODRO, []) if r["maturity_date"] == SERIES_EXPIRY]
    for code in TARGETS.keys() & rows.keys():
        watch.require(rows[code]["underlying_code"] == KHODRO and rows[code]["maturity_date"] == SERIES_EXPIRY
                      and rows[code]["option_type"] == "CALL", f"unexpected identity for خودرو target: {code}")
    return {
        "capture_id": f"manual:{meta['sequence']}", "body_sha256": meta["body_sha256"], "seal_sha256": meta["head_sha256"],
        **{key: meta[key] for key in ("receipt_tehran", "receipt_utc", *TIME_FIELDS)},
        "screening_status": "existing_replay" if available else "not_evaluated_invalid_snapshot_numeric_fields",
        "summary": summarize(rows.values(), available),
        "by_type": {k: summarize((r for r in rows.values() if r["option_type"] == k), available) for k in ("CALL", "PUT")},
        "by_underlying_id": groups,
        "khodro_series": {
            "underlying_code": KHODRO, "maturity_date": SERIES_EXPIRY,
            "summary": summarize(series, available),
            "by_type": {k: summarize((r for r in series if r["option_type"] == k), available) for k in ("CALL", "PUT")},
            "targets": {code: rows.get(code) for code in TARGETS},
        },
        "contracts_by_id": rows,
    }


def compare(old, new):
    common = sorted(old.keys() & new.keys())
    paired = 0
    changes = dict.fromkeys(("bid_or_ask", "spread", "depth"), 0)
    transitions = {k: Counter() for k in QUOTE_CLASSES}
    for code in common:
        a, b = old[code], new[code]
        watch.require(identity(a) == identity(b), f"instrument identity changed: {code}")
        left, right = a["quote_class"], b["quote_class"]
        transitions[left][right] += 1
        if left == right == "two_sided":
            paired += 1
            changes["bid_or_ask"] += any(a[k] != b[k] for k in watch.PRICE_FIELDS)
            # Exact rational relationship; don't use rounded displayed spreads to detect changes.
            changes["spread"] += a["best_ask"] * b["best_bid"] != b["best_ask"] * a["best_bid"]
            changes["depth"] += any(a[k] != b[k] for k in ("bid_volume", "ask_volume"))
    return {
        "common_same_identity": len(common), "appeared_ids": sorted(new.keys() - old.keys()),
        "disappeared_ids": sorted(old.keys() - new.keys()), "quote_class_transitions": transitions,
        "two_sided_at_both_count": paired, "two_sided_at_both_changes": changes,
        "trade_counter_deltas_reported": False, "equivalent_full_day_activity": False,
    }


def build_report(stream=collector.STREAM):
    with collector.locked(stream, exclusive=False):
        records = collector.read_stream(stream)
        observations = []
        for seq, day in CAPTURES:
            watch.require(seq <= len(records), f"missing pinned capture manual:{seq}")
            meta = records[seq - 1]
            watch.require(watch.parse_time(meta["receipt_tehran"]).date().isoformat() == day,
                          f"unexpected Tehran date for manual:{seq}")
            observations.append(read_capture(meta))
    registry = evidence.current_assessment()
    names = (*watch.MODEL_FILES, "shadow_watch.py", "shadow_collector.py", "greeks_evidence_registry.py", Path(__file__).name)
    return {
        "schema_version": 1, "dataset_class": watch.DATA_CLASS, "source_url": watch.SOURCE,
        "execution_eligible": False, "greeks_assumption_status": "experimental_unverified",
        "time_basis": "pinned_manual_HTTP_receipts_not_exchange_times_or_closes",
        "price_unit": "raw_feed_unit_unverified", "underlying_synchrony_verified": False,
        "evidence_registry": {key: registry[key] for key in ("registry_version", "sha256")},
        "code_sha256": {name: watch.digest((watch.ROOT / name).read_bytes()) for name in names},
        "thresholds_from_existing_rules": {"minimum_trades": MIN_TRADES, "screen_spread_percent": MAX_SPREAD,
                                           "rank_spread_percent": RANK_SPREAD},
        "observations": observations, "comparison": compare(*(o["contracts_by_id"] for o in observations)),
    }


def fmt(value):
    return "—" if value is None else str(value)


def label(value):
    return " ".join((value or "نامشخص").split()).replace("|", "&#124;").replace("<", "&lt;")


def counts(summary):
    quote, gates = summary["quote_classes"], summary["screening"]
    return " | ".join(map(fmt, (summary["contracts"], quote["two_sided"], quote["bid_only"] + quote["ask_only"],
                             quote["missing"], quote["crossed"] + quote["inconsistent"],
                             summary["insufficient_liquidity"], summary["missing_or_invalid_underlying_reference"],
                             summary["median_ordered_price_spread_percent"],
                             gates["passes_existing_screen"] if gates else None)))


def markdown(report):
    header = "| گروه | کل | دوطرفه | یک‌طرفه | بی‌مظنه | متقاطع/ناسازگار | کم‌نقدشونده | قیمت پایه ناقص | میانهٔ اسپرد٪ | عبور غربال |"
    rule = "| --- |" + " ---: |" * 9
    lines = [
        "# ممیزی آفلاین کیفیت دادهٔ اختیار TSETMC", "",
        "**صرفاً مشاهده؛ `experimental_unverified`، غیرقابل اجرا.** زمان‌ها دریافت محلی HTTP در تهران‌اند، "
        "نه مهر زمانی مظنه و نه پایان جلسه. این دو ثبت دستی‌اند؛ ثبت‌های صبح روز اول وارد مقایسه نشده‌اند.", "",
        "بازتولید: `python -B market_data_quality.py --format markdown` یا `--format json`؛ "
        "هش بدنه، مهر زنجیره، کد و رجیستری شواهد در JSON ثبت شده‌اند. هش اصالت بازار را ثابت نمی‌کند.", "",
        "دوطرفه یعنی bid و ask مثبت و مرتب با عمق مثبت هر دو طرف؛ قیمت برابر متقاطع نیست. قیمت مثبت بدون عمق "
        "یا عمق مثبت بدون قیمت، ناسازگار است. میانهٔ اسپرد از قیمت‌های مثبت و مرتب، مستقل از عمق است. "
        "کم‌نقدشونده یعنی حداقل یک مورد: عمق نامثبت/ناموجود، شمار قابل استفاده کمتر از حد غربال یا حجم تجمعی نامثبت/ناموجود. "
        "این شاخص‌ها هم‌پوشان‌اند و نقدشوندگی اجرایی را اثبات نمی‌کنند.", "",
        "قیمت آخر پایه ورودی مدل و قیمت پایانی پایه ورودی غربال است. مثبت بودن آن‌ها یا حضور در یک پاسخ، "
        "اصالت، تازگی یا هم‌زمانی را ثابت نمی‌کند؛ اختلاف قیمت آخر و پایانی نیز به‌خودی‌خود خطا نیست.", "",
    ]
    for obs in report["observations"]:
        s = obs["summary"]
        q = s["quote_classes"]
        lines += [f"## {obs['capture_id']} — `{obs['receipt_tehran']}`", "",
                  header, rule, f"| همه | {counts(s)} |"]
        lines.extend(f"| {k} | {counts(v)} |" for k, v in obs["by_type"].items())
        series = obs["khodro_series"]
        lines.append(f"| سری خودرو `{SERIES_EXPIRY}` | {counts(series['summary'])} |")
        lines.extend(f"| همان سری / {k} | {counts(v)} |" for k, v in series["by_type"].items())
        lines += ["", f"- فقط خرید / فقط فروش: {q['bid_only']} / {q['ask_only']}؛ متقاطع / سایر ناسازگار: "
                  f"{q['crossed']} / {q['inconsistent']}؛ قیمت برابر: {s['locked_prices']}. شمار/حجم متناقض: "
                  f"{s['trade_counter_inconsistent']}؛ اسپرد بالاتر از سقف غربال: {s['spread_above_screening_limit']}.",
                  f"- بدون زمان بورس / زمان ارائه‌دهنده / توالی: {s['missing_exchange_quote_timestamp']} / "
                  f"{s['missing_provider_quote_timestamp']} / {s['missing_order_book_sequence']}؛ "
                  f"{s['underlying_reference_present_but_unverified']} ردیف با هر دو مرجع پایهٔ مثبت ولی تأییدنشده؛ "
                  f"{s['quote_provenance_blocked']} قرارداد از نظر منشأ مظنه برای اجرا مسدودند."]
        varied = [label(" / ".join(v["symbols"])) + f" `{code}`"
                  for code, v in obs["by_underlying_id"].items()
                  if any(len(values) > 1 for values in v["reference_values"].values())]
        lines.append("- پایه با بیش از یک مقدار مثبت برای یک نوع مرجع در یک دریافت: "
                     + ("، ".join(varied) or "هیچ") + "؛ این فقط آزمون سازگاری داخلی است.")
        if (g := s["screening"]) is not None:
            reasons = "؛ ".join(f"{reason}: {n}" for reason, n in sorted(g["rank_first_rejections"].items())) or "—"
            lines += [f"- از {g['passes_existing_screen']} عبوری غربال، {g['candidate_quote_provenance_blocked']} "
                      f"مانع منشأ و {g['candidate_model_unverified']} مدل تأییدنشده دارند (هم‌پوشان)؛ "
                      f"اولین رد رتبه برای {g['first_rank_rejection_model_unverified']} مورد فرض‌های مدل بود؛ "
                      f"واجد رتبه: {g['rank_eligible']}. اولین ردهای رتبه: {reasons}."]
        else:
            lines += ["- به علت فیلد عددی نامعتبر، بازپخش غربال و رتبه برای این دریافت انجام نشد."]
        lines += ["", "### به تفکیک دارایی پایه", "", header, rule]
        for code, v in obs["by_underlying_id"].items():
            lines.append(f"| {label(' / '.join(v['symbols']))} `{code}` | {counts(v)} |")
        lines += ["", "### دو نامزد اولیهٔ خودرو", "",
                  "| نماد / شناسه | اعمال / ضریب خام | خرید / فروش | عمق خرید / فروش | اسپرد٪ | شمار / حجم تجمعی همان دریافت | آخر / پایانی پایه | غربال / اولین رد رتبه |",
                  "| --- | --- | --- | --- | ---: | --- | --- | --- |"]
        for code, symbol in TARGETS.items():
            r = series["targets"][code]
            name = f"{symbol} `{code}`"
            if r is None:
                lines.append(f"| {name} | غایب | — | — | — | — | — | — |")
                continue
            g = r["screening"]
            gate = ("بررسی‌نشده" if not g else
                    f"{'عبور' if g['candidate_eligible'] else 'رد'} / {g['rank_rejection'] or g['candidate_rejection'] or '—'}")
            pairs = [f"{fmt(r[a])} / {fmt(r[b])}" for a, b in
                     (("strike_price", "contract_size"), ("best_bid", "best_ask"), ("bid_volume", "ask_volume"),
                      ("trade_count", "trade_volume"), ("underlying_last_price", "underlying_closing_price"))]
            lines.append(f"| {name} | {' | '.join(pairs[:3])} | {fmt(r['spread_percent'])} | {' | '.join(pairs[3:])} | {gate} |")
        lines.append("")
    c = report["comparison"]
    changes = c["two_sided_at_both_changes"]
    lines += ["## مقایسهٔ دو دریافت", "",
              f"- {c['common_same_identity']} شناسهٔ مشترک با هویت خام ثابت؛ "
              f"{len(c['appeared_ids'])} ظاهر و {len(c['disappeared_ids'])} غایب. "
              f"از {c['two_sided_at_both_count']} قرارداد دوطرفه در هر دو ثبت، "
              f"خرید/فروش در {changes.get('bid_or_ask', 0)}، اسپرد در {changes.get('spread', 0)} "
              f"و عمق در {changes.get('depth', 0)} مورد تغییر کرد. این‌ها اختلاف مشاهده‌اند، نه بازده یا کیفیت اجرای سفارش.",
              "- شمار و حجم تجمعی فقط در هر دریافت تفسیر می‌شوند: ساعت‌ها متفاوت‌اند و این گزارش تفاضل آن‌ها، "
              "فعالیت هم‌ارز روزانه یا تغییر نرخ معامله را محاسبه نمی‌کند.",
              "- پیش‌نیاز اعتماد: فید معتبر زمان‌دار و هم‌زمان پایه/اختیار، تطبیق مستقل شناسه و واحد و ضریب، "
              "و اعتبارسنجی مدل؛ [شکاف شواهد](greeks-evidence-gaps-v1.fa.md)."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", type=Path, default=collector.STREAM)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.stream)
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2))
        else:
            sys.stdout.write(markdown(report))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Offline market data quality report stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
