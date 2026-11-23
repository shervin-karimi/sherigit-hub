import argparse
from collections import Counter, defaultdict
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from subprocess import TimeoutExpired
from zoneinfo import ZoneInfoNotFoundError

BASE = Path(__file__).parent
RANKED_FILE = BASE / "data" / "ranked_candidates.json"
TRENDS_FILE = BASE / "data" / "underlying_trends_v2.json"

OUTPUT_JSON = BASE / "data" / "combined_analysis.json"
OUTPUT_TXT = BASE / "data" / "گزارش_تطبیقی.txt"

MAX_AGE_HOURS = 36
EXPECTED_MODEL = "2.1-experimental"
RESEARCH_CAPTURE = "manual:2"
RESEARCH_UNDERLYING = "65883838195688438"

# The existing shortlist treats absent values as zero. Keep its verdict, but
# distinguish missing evidence from a definite rejection in this report.
SCREEN_FIELDS = {
    "invalid_quote_or_closing_spot": ("best_bid", "best_ask", "underlying_closing_price"),
    "insufficient_trades": ("trade_count",),
    "no_trade_volume": ("trade_volume",),
    "no_bid_depth": ("bid_volume",),
    "no_ask_depth": ("ask_volume",),
    "near_expiry": ("remaining_days",),
}
SCREEN_REASONS_FA = {
    "invalid_quote_or_closing_spot": "مظنهٔ دوطرفه یا قیمت پایانی پایه برای غربال معتبر نیست.",
    "insufficient_trades": "تعداد معاملات کمتر از حد غربال است.",
    "no_trade_volume": "حجم معامله مثبت نیست.",
    "no_bid_depth": "عمق خرید مثبت نیست.",
    "no_ask_depth": "عمق فروش مثبت نیست.",
    "near_expiry": "شمار روزهای مانده کمتر از حد غربال است.",
    "wide_spread": "اسپرد از سقف غربال عبور کرده است.",
    "ask_below_intrinsic": "مظنهٔ فروش در غربال از ارزش ذاتی محاسباتی کمتر است.",
}
RESEARCH_STATES = ("REJECTED", "UNKNOWN", "RESEARCH_ONLY")
OPTION_TYPES = ("CALL", "PUT")
RESEARCH_LIMITATIONS = (
    "زمان دریافت HTTP نه زمان مظنه است و نه مدرک هم‌زمانی قیمت پایه و اختیار.",
    "برش روزانه مانع ورود تاریخ‌های بعدی است؛ زمان انتشار، تعدیل و کامل‌بودن جلسات تأیید نشده‌اند.",
    "واحد قیمت و ضریب مؤثر، مهلت معامله و قواعد اعمال و تسویه مستقلاً تأیید نشده‌اند.",
    "غربال اولیه و گیت رتبه جدا هستند؛ محاسبهٔ IV اعتبار مدل را ثابت نمی‌کند.",
)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + "."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def parse_timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def age_is_valid(hours):
    try:
        return 0 <= float(hours) <= MAX_AGE_HOURS
    except (TypeError, ValueError):
        return False


def format_number(value, digits=2):
    try:
        if value is None:
            return "-"
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def build_trend_map(trend_data):
    metadata = trend_data.get("metadata", {})
    generated = parse_timestamp(metadata.get("generated_at"))

    metadata_age_ok = False
    if generated:
        age = (
            datetime.now(timezone.utc) - generated
        ).total_seconds() / 3600
        metadata_age_ok = 0 <= age <= MAX_AGE_HOURS

    model_ok = metadata.get("model_version") == EXPECTED_MODEL
    results = trend_data.get("results", [])

    if not isinstance(results, list):
        raise ValueError("ساختار results در فایل روند معتبر نیست.")

    trend_map = {}
    duplicate_symbols = set()

    for trend in results:
        symbol = trend.get("symbol")
        if not symbol:
            continue
        if symbol in trend_map:
            duplicate_symbols.add(symbol)
        trend_map[symbol] = trend

    for symbol in duplicate_symbols:
        trend_map.pop(symbol, None)

    return trend_map, {
        "generated_at": metadata.get("generated_at"),
        "model_version": metadata.get("model_version"),
        "metadata_age_ok": metadata_age_ok,
        "model_ok": model_ok,
        "symbol_count": len(trend_map),
        "duplicate_symbols": sorted(duplicate_symbols),
    }


def main():
    ranked = load_json(RANKED_FILE)
    trend_data = load_json(TRENDS_FILE)

    if not isinstance(ranked, list):
        raise ValueError("ساختار ranked_candidates.json باید فهرست باشد.")

    trend_map, trend_meta = build_trend_map(trend_data)

    report = []

    for contract in ranked:
        underlying = contract.get("underlying_symbol")
        trend = trend_map.get(underlying)

        trend_name = trend.get("trend", "نامشخص") if trend else "نامشخص"
        trend_date = trend.get("latest_date") if trend else None
        price_date = contract.get("underlying_price_date")

        reasons = []

        if not trend:
            reasons.append("روند معتبر برای دارایی پایه پیدا نشد")
        else:
            if trend.get("analysis_status") != "ok":
                reasons.append("وضعیت تحلیل روند معتبر نیست")

            if not trend_meta["model_ok"]:
                reasons.append("نسخه مدل روند مورد انتظار نیست")

            if not trend_meta["metadata_age_ok"]:
                reasons.append("زمان تولید تحلیل روند نامعتبر یا قدیمی است")

            if not age_is_valid(trend.get("history_age_hours")):
                reasons.append("تاریخچه دارایی پایه قدیمی یا نامعتبر است")

            if trend_name not in ("صعودی", "نزولی", "خنثی"):
                reasons.append("جهت روند ناشناخته است")

            try:
                dates_match = (
                    trend_date is not None
                    and price_date is not None
                    and int(trend_date) == int(price_date)
                )
            except (TypeError, ValueError):
                dates_match = False

            if not dates_match:
                reasons.append("تاریخ قیمت پایه با آخرین تاریخ روند یکسان نیست")

        option_type = contract.get("option_type")
        alignment = "غیرقابل ارزیابی"
        directional_eligible = False

        if not reasons and trend:
            if trend_name == "خنثی":
                alignment = "بدون جهت مشخص"
            elif trend_name == "صعودی" and option_type == "CALL":
                alignment = "هم‌جهت"
                directional_eligible = True
            elif trend_name == "نزولی" and option_type == "PUT":
                alignment = "هم‌جهت"
                directional_eligible = True
            elif option_type not in ("CALL", "PUT"):
                reasons.append("نوع قرارداد ناشناخته است")
            else:
                alignment = "ناهم‌جهت"

        if reasons:
            data_status = "رد بررسی: " + "؛ ".join(reasons)
        else:
            data_status = "تاریخ و کیفیت اولیه داده‌ها تأیید شد"

        item = {
            "rank": contract.get("rank"),
            "symbol": contract.get("symbol"),
            "option_type": option_type,
            "underlying_symbol": underlying,
            "trend": trend_name,
            "trend_date": trend_date,
            "underlying_price_date": price_date,
            "alignment": alignment,
            "directional_eligible": directional_eligible,
            "data_status": data_status,
            "analysis_status": trend.get("analysis_status") if trend else None,
            "history_age_hours": trend.get("history_age_hours") if trend else None,
            "spread_percent": contract.get("spread_percent"),
            "trade_count": contract.get("trade_count"),
            "trade_volume": contract.get("trade_volume"),
            "best_bid": contract.get("best_bid"),
            "best_ask": contract.get("best_ask"),
            "bid_volume": contract.get("bid_volume"),
            "ask_volume": contract.get("ask_volume"),
            "delta": contract.get("delta"),
            "iv_mid": contract.get("iv_mid"),
            "iv_status": contract.get("iv_status"),
            "theta_per_day": contract.get("theta_per_day"),
            "remaining_days": contract.get("remaining_days"),
            "distance_from_ma20_percent": (
                trend.get("distance_from_ma20_percent") if trend else None
            ),
        }

        report.append(item)

    report.sort(key=lambda x: (
        not x["directional_eligible"],
        x["rank"] if isinstance(x["rank"], (int, float)) else 999999
    ))

    atomic_write(
        OUTPUT_JSON,
        json.dumps(report, ensure_ascii=False, indent=2)
    )

    aligned = [x for x in report if x["alignment"] == "هم‌جهت"]
    eligible = [x for x in report if x["directional_eligible"]]
    neutral = [x for x in report if x["alignment"] == "بدون جهت مشخص"]
    opposite = [x for x in report if x["alignment"] == "ناهم‌جهت"]
    rejected = [x for x in report if x["data_status"].startswith("رد بررسی:")]

    lines = [
        "گزارش تطبیقی قراردادهای اختیار معامله",
        "=" * 50,
        f"تعداد کل قراردادها: {len(report)}",
        f"قراردادهای هم‌جهت: {len(aligned)}",
        f"واجد شرایط بررسی جهت‌دار: {len(eligible)}",
        f"قراردادهای بدون جهت مشخص: {len(neutral)}",
        f"قراردادهای ناهم‌جهت: {len(opposite)}",
        f"قراردادهای ردشده به علت کیفیت یا تاریخ داده: {len(rejected)}",
        "",
        f"نسخه مدل روند: {trend_meta['model_version']}",
        f"زمان تولید تحلیل روند: {trend_meta['generated_at']}",
        f"تازگی تحلیل کلی: {'معتبر' if trend_meta['metadata_age_ok'] else 'نامعتبر'}",
        "",
        "هشدار مهم:",
        "این گزارش سیگنال خرید یا فروش نیست.",
        "هم‌جهت بودن به معنی مناسب بودن معامله نیست.",
        "تازگی مظنه خود قرارداد اختیار نیز در این مرحله تأیید نشده است.",
        "تا بررسی نقدشوندگی، قیمت اجرایی، ریسک و تازگی مظنه معامله نکنید.",
        "",
        "قراردادها:",
        "-" * 50,
    ]

    for item in report[:30]:
        option_name = (
            "اختیار خرید" if item["option_type"] == "CALL"
            else "اختیار فروش" if item["option_type"] == "PUT"
            else "نامشخص"
        )

        lines.extend([
            f"نماد: {item['symbol']} | پایه: {item['underlying_symbol']}",
            f"نوع: {option_name} | رتبه: {item['rank']}",
            f"روند: {item['trend']} | تطبیق: {item['alignment']}",
            f"واجد شرایط بررسی جهت‌دار: {'بله' if item['directional_eligible'] else 'خیر'}",
            f"وضعیت داده: {item['data_status']}",
            f"تاریخ روند: {item['trend_date']} | تاریخ قیمت پایه: {item['underlying_price_date']}",
            f"سن تاریخچه (ساعت): {format_number(item['history_age_hours'])}",
            f"فاصله مظنه: {format_number(item['spread_percent'])} درصد",
            f"تعداد معاملات: {item['trade_count']} | حجم: {item['trade_volume']}",
            f"Bid: {item['best_bid']} | Ask: {item['best_ask']}",
            f"حجم Bid: {item['bid_volume']} | حجم Ask: {item['ask_volume']}",
            f"دلتا: {format_number(item['delta'], 4)} | IV: {format_number(item['iv_mid'])}",
            f"وضعیت IV: {item['iv_status']} | تتا روزانه: {format_number(item['theta_per_day'])}",
            f"روز باقی‌مانده تا سررسید: {item['remaining_days']}",
            "-" * 50,
        ])

    atomic_write(OUTPUT_TXT, "\n".join(lines))

    print("گزارش تطبیقی ساخته شد.")
    print("تعداد کل قراردادها:", len(report))
    print("هم‌جهت:", len(aligned))
    print("واجد شرایط بررسی جهت‌دار:", len(eligible))
    print("بدون جهت مشخص:", len(neutral))
    print("ناهم‌جهت:", len(opposite))
    print("ردشده به علت کیفیت یا تاریخ داده:", len(rejected))
    print("تازگی تحلیل کلی:", "معتبر" if trend_meta["metadata_age_ok"] else "نامعتبر")
    print("فایل داده:", OUTPUT_JSON)
    print("گزارش فارسی:", OUTPUT_TXT)
    print()
    print("این گزارش به‌تنهایی مجوز معامله نیست.")


def research_status(status, code, explanation):
    return {"status": status, "reason_code": code, "reason_fa": explanation}


def history_index(path):
    import shadow_watch as watch

    if not path.exists():
        return None, {}, {}, research_status("UNKNOWN", "history_file_missing", "فایل تاریخچهٔ پایه در دسترس نیست.")
    try:
        data, raw = watch.read_object(path, watch.MAX_BODY)
    except OSError:
        return None, {}, {}, research_status("UNKNOWN", "history_unreadable", "خواندن تاریخچهٔ پایه ممکن نیست.")
    except ValueError:
        return None, {}, {}, research_status("REJECTED", "history_file_invalid", "فایل تاریخچهٔ پایه ساختار معتبر ندارد.")
    digest = watch.digest(raw)
    symbols, metadata = data.get("symbols"), data.get("metadata")
    if not isinstance(symbols, dict) or not isinstance(metadata, dict):
        return digest, {}, {}, research_status("UNKNOWN", "history_schema_incomplete",
                                               "ساختار پایه‌ها یا فرادادهٔ تاریخچه ناقص است.")
    index = defaultdict(list)
    for name, item in symbols.items():
        if isinstance(item, dict) and watch.positive_id(item.get("code")):
            index[item["code"]].append((name, item))
    return digest, metadata, index, None


def indexed_history(indexed, code, receipt):
    import analyze_underlying_trends_v2 as trends

    digest, metadata, index, error = indexed
    result = {"ins_code": code, "symbol": None, "history": None, "history_sha256": digest}

    def finish(status, reason, message):
        return result | research_status(status, reason, message)

    if error:
        return result | error
    matches = index.get(code, ())
    if not matches:
        return finish("UNKNOWN", "history_id_not_found", "تاریخچه‌ای با شناسهٔ دقیق پایه پیدا نشد.")
    if len(matches) != 1:
        return finish("UNKNOWN", "history_id_ambiguous", "چند ورودی تاریخچه همان شناسهٔ پایه را دارند.")
    name, item = matches[0]
    result["symbol"] = name
    downloaded = trends.parse_time(item.get("fetched_at"))
    if not isinstance(item.get("history"), list) or downloaded is None:
        return finish("UNKNOWN", "history_evidence_incomplete", "رکوردها یا زمان دریافت تاریخچه در دسترس نیستند.")
    result["downloaded_after_capture"] = downloaded > receipt
    try:
        report = trends.research_report(name, item, metadata, before=receipt.date(), expected_code=code)
    except (ValueError, ArithmeticError) as error:
        return finish("REJECTED", "history_records_invalid", f"رکوردهای تاریخچه معتبر یا سازگار نیستند: {error}")
    result["history"] = report
    if not any(m["status"] == "COMPUTED" for m in report["indicators"].values()):
        return finish("UNKNOWN", "insufficient_history", "مشاهدهٔ قابل استفاده برای محاسبهٔ شاخص کافی نیست.")
    return finish("RESEARCH_ONLY", "retrospective_history", "شاخص‌ها توصیفی‌اند؛ دسترسی تاریخی اثبات نشده است.")


def research_history(path, code, receipt):
    return indexed_history(history_index(path), code, receipt)


def research_option(ins_code, row, history):
    fields, screening = row["field_status"], row["screening"]
    if "invalid" in fields.values() or row["quote_class"] == "crossed":
        result = research_status("REJECTED", "invalid_option_observation", "فیلد عددی نامعتبر یا مظنهٔ متقاطع دیده شد.")
    elif row["quote_class"] == "inconsistent":
        missing = any(fields[key] == "missing" for key in ("best_bid", "best_ask", "bid_volume", "ask_volume"))
        result = (research_status("UNKNOWN", "quote_evidence_missing", "قیمت یا عمق مظنه ناقص است.") if missing else
                  research_status("REJECTED", "price_depth_conflict", "قیمت و عمق ثبت‌شده با هم سازگار نیستند."))
    elif screening is None:
        result = research_status("UNKNOWN", "screening_unavailable", "نتیجهٔ غربال معتبر برای این ثبت در دسترس نیست.")
    elif not screening["candidate_eligible"]:
        reason = screening["candidate_rejection"]
        if any(fields[key] == "missing" for key in SCREEN_FIELDS.get(reason, ())):
            result = research_status("UNKNOWN", "screening_input_missing", "ورودی لازم برای حکم غربال ناقص است.")
        else:
            result = research_status("REJECTED", reason, SCREEN_REASONS_FA.get(reason, "رد قطعی در غربال موجود."))
    elif history["status"] != "RESEARCH_ONLY":
        result = {key: history[key] for key in ("status", "reason_code", "reason_fa")}
    else:
        result = research_status("RESEARCH_ONLY", "descriptive_comparison",
                                 "مقایسهٔ توصیفی ممکن است؛ این نتیجه مجوز یا سیگنال معامله نیست.")
    return row | result | {
        "ins_code": ins_code, "identity_status": "OBSERVED_UNVERIFIED",
        "price_unit": "UNKNOWN", "multiplier_unit": "UNKNOWN", "depth_unit": "UNKNOWN",
        "trade_cutoff_status": "UNKNOWN", "exercise_status": "UNKNOWN", "settlement_status": "UNKNOWN",
        "execution_eligible": False,
    }


def build_research_report(capture_id, underlying_code, *, stream, history_path):
    import greeks_evidence_registry as evidence
    import market_data_quality as quality
    import shadow_collector as collector
    import shadow_watch as watch

    watch.require((capture_id, underlying_code) == (RESEARCH_CAPTURE, RESEARCH_UNDERLYING),
                  "فقط ثبت manual:2 و شناسهٔ مصوب خودرو در دامنهٔ این نمونه‌اند.")
    with collector.locked(stream, exclusive=False):
        records = collector.read_stream(stream, dataset_class=watch.DATA_CLASS)
        meta = next((item for item in records if f"manual:{item['sequence']}" == capture_id), None)
        watch.require(meta is not None, f"ثبت درخواستی پیدا نشد: {capture_id}")
        # read_capture rejects duplicate IDs/contradictory identities before making a map.
        observed = quality.read_capture(meta)
    receipt = watch.parse_time(meta["receipt_tehran"])
    history = research_history(history_path, underlying_code, receipt)
    registry = evidence.current_assessment()
    options = [research_option(code, row, history) for code, row in sorted(observed["contracts_by_id"].items())
               if row["underlying_code"] == underlying_code]
    limitations = list(RESEARCH_LIMITATIONS)
    if not options:
        limitations.append("در ثبت درخواستی هیچ اختیار با شناسهٔ دقیق این پایه پیدا نشد.")
    return {
        "schema_version": 1, "research_only": True, "execution_eligible": False,
        "point_in_time_verified": False, "capture_date_cutoff_exclusive": receipt.date().isoformat(),
        "capture": {key: observed[key] for key in ("capture_id", "receipt_tehran", "receipt_utc", "body_sha256",
                                                   "seal_sha256", "screening_status", *quality.TIME_FIELDS)} | {
            "dataset_class": meta["dataset_class"], "source_url": meta["source_url"],
            "quote_freshness": "UNKNOWN", "spot_quote_synchrony_verified": False,
        },
        "underlying": history, "options": options,
        "option_counts": {kind: sum(row["option_type"] == kind for row in options) for kind in ("CALL", "PUT")},
        "greeks_assumption_status": registry["greeks_assumption_status"],
        "greeks_evidence": {key: registry[key] for key in ("registry_version", "sha256", "topics")},
        "risk": {"status": "UNKNOWN", "max_quantity": None},
        "limitations_fa": limitations,
    }


def read_market_capture(capture_id, stream, legacy_archive):
    import market_data_quality as quality
    import shadow_collector as collector
    import shadow_watch as watch

    match = re.fullmatch(r"(manual|legacy):([1-9][0-9]{0,7})", capture_id)
    watch.require(match is not None, "شناسهٔ دقیق ثبت لازم است: manual:N یا legacy:N")
    family, number = match[1], int(match[2])
    if family == "manual":
        with collector.locked(stream, exclusive=False):
            records = collector.read_stream(stream, dataset_class=watch.DATA_CLASS)
            meta = next((r for r in records if r["sequence"] == number), None)
            watch.require(meta is not None, f"ثبت درخواستی پیدا نشد: {capture_id}")
            observed = quality.read_capture(meta, diagnostics=True)
        provenance = {"integrity_method": "manual_hash_chain", "manifest_sha256": None}
    else:
        watch.require(legacy_archive.is_dir() and not legacy_archive.is_symlink(), "مسیر آرشیو قدیمی معتبر نیست.")
        manifest_path = legacy_archive / "manifest.json"
        _, manifest = watch.read_object(manifest_path, watch.MAX_META)
        digest = watch.digest(manifest)
        records = watch.records_in_order(legacy_archive)
        selected = next(((path, r) for _, attempt, path, r in records if attempt == number), None)
        watch.require(selected is not None, f"ثبت درخواستی پیدا نشد: {capture_id}")
        path, receipt = selected
        meta = receipt | {"path": path}
        observed = quality.read_capture(meta, legacy=True, diagnostics=True)
        watch.read_object(manifest_path, watch.MAX_META, digest)
        provenance = {"integrity_method": "legacy_manifest_and_receipt", "manifest_sha256": digest}
    capture = {key: observed[key] for key in ("capture_id", "receipt_tehran", "receipt_utc", "body_sha256",
                                              "seal_sha256", "screening_status", *quality.TIME_FIELDS)}
    return observed, capture | provenance | {
        "dataset_class": meta["dataset_class"], "source_url": meta["source_url"],
        "quote_freshness": "UNKNOWN", "spot_quote_synchrony_verified": False,
    }


def market_counts(options, screening_available):
    types = Counter(row["option_type"] for row in options)
    statuses = Counter(row["status"] for row in options)
    passes = Counter(row["option_type"] for row in options if (row["screening"] or {}).get("candidate_eligible"))
    return {
        "option_counts": {kind: types[kind] for kind in OPTION_TYPES},
        "research_counts": {status: statuses[status] for status in RESEARCH_STATES},
        "screening": {
            "evaluated": screening_available, "candidate_passes": sum(passes.values()) if screening_available else None,
            "candidate_passes_by_type": ({kind: passes[kind] for kind in OPTION_TYPES} if screening_available else None),
            "strict_passes": (sum(bool((row["screening"] or {}).get("rank_eligible")) for row in options)
                              if screening_available else None),
        },
    }


def build_market_research_report(capture_id, *, stream, legacy_archive, history_path):
    import greeks_evidence_registry as evidence
    import shadow_watch as watch

    observed, capture = read_market_capture(capture_id, stream, legacy_archive)
    receipt = watch.parse_time(capture["receipt_tehran"])
    indexed = history_index(history_path)
    history = {code: indexed_history(indexed, code, receipt) for code in observed["by_underlying_id"]}
    evaluated = capture["screening_status"] == "existing_replay"
    options, grouped = [], defaultdict(list)
    for ins_code, row in sorted(observed["contracts_by_id"].items()):
        code = row["underlying_code"]
        option = research_option(ins_code, row, history[code])
        grouped[code].append(option)
        options.append(option)

    underlyings = {}
    for code, group in observed["by_underlying_id"].items():
        technical = history[code]
        underlyings[code] = {"symbols": group["symbols"], **market_counts(grouped[code], evaluated),
                             "technical_analysis": technical | {
                                 "status": "AVAILABLE" if technical["status"] == "RESEARCH_ONLY" else "NOT_AVAILABLE",
                             }}
    counts = market_counts(options, evaluated)
    registry = evidence.current_assessment()
    return {
        "schema_version": 2, "research_only": True, "execution_eligible": False, "point_in_time_verified": False,
        "capture_date_cutoff_exclusive": receipt.date().isoformat(), "capture": capture,
        "summary": counts | {"underlying_count": len(underlyings), "contract_count": len(options),
                             "technical_available_count": sum(u["technical_analysis"]["status"] == "AVAILABLE"
                                                              for u in underlyings.values())},
        "reconciliation": observed["reconciliation"], "diagnostics": observed["diagnostics"],
        "underlyings": underlyings, "options": options,
        "greeks_assumption_status": registry["greeks_assumption_status"],
        "greeks_evidence": {key: registry[key] for key in ("registry_version", "sha256", "topics")},
        "risk": {"status": "UNKNOWN", "max_quantity": None}, "limitations_fa": list(RESEARCH_LIMITATIONS),
    }


def market_summary(report):
    summary, counts, capture = report["summary"], report["reconciliation"], report["capture"]
    types, states, screening = summary["option_counts"], summary["research_counts"], summary["screening"]
    passed_types = screening["candidate_passes_by_type"]
    return "\n".join((
        f"پژوهش کل بازار — ثبت {capture['capture_id']}، دریافت HTTP تهران: {capture['receipt_tehran']}",
        f"ردیف خام: {counts['raw_paired_rows']}؛ سمت خالی: {counts['empty_sides']}؛ "
        f"سمت فقط تشخیصی: {counts['diagnostic_only_sides']}؛ کل یافته‌های تشخیصی: {len(report['diagnostics'])}",
        f"اختیار یکتا: {summary['contract_count']} (CALL: {types['CALL']}، PUT: {types['PUT']})؛ پایه: "
        f"{summary['underlying_count']}؛ دارای تحلیل فنی: {summary['technical_available_count']}",
        (f"عبور از غربال اولیه: {screening['candidate_passes']} (CALL: {passed_types['CALL']}، PUT: {passed_types['PUT']})؛ "
         f"عبور از رتبه‌بندی سخت‌گیرانه: {screening['strict_passes']}"
         if screening["evaluated"] else f"غربال و رتبه‌بندی انجام نشد: {capture['screening_status']}"),
        "وضعیت پژوهشی: " + "، ".join(f"{status}: {states[status]}" for status in RESEARCH_STATES),
        "آرشیوی و غیرقابل اجرا؛ زمان مظنه و سقف تعداد نامعلوم، Greeks آزمایشی و تأییدنشده.",
        "جزئیات ردیف‌ها: --format json",
    ))


def research_main(argv):
    import shadow_collector as collector
    import shadow_watch as watch

    parser = argparse.ArgumentParser(description="مقایسهٔ گذشته‌نگر پژوهشی پایه و اختیار؛ بدون سیگنال یا اجرا")
    parser.add_argument("--research", action="store_true", required=True)
    parser.add_argument("--capture", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--underlying-code", choices=(RESEARCH_UNDERLYING,))
    mode.add_argument("--all-underlyings", action="store_true")
    parser.add_argument("--format", choices=("summary", "json"))
    parser.add_argument("--stream", type=Path, default=collector.STREAM)
    parser.add_argument("--legacy-archive", type=Path, default=watch.DEFAULT_ARCHIVE)
    parser.add_argument("--history", type=Path, default=BASE / "data" / "underlying_history.json")
    args = parser.parse_args(argv)
    if not args.all_underlyings and (args.capture != RESEARCH_CAPTURE or args.format is not None):
        parser.error("حالت تک‌پایه فقط ثبت manual:2 و خروجی JSON نسخهٔ ۱ را می‌پذیرد.")
    try:
        if args.all_underlyings:
            report = build_market_research_report(args.capture, stream=args.stream,
                                                  legacy_archive=args.legacy_archive, history_path=args.history)
            output = (json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True)
                      if args.format == "json" else market_summary(report))
        else:
            report = build_research_report(args.capture, args.underlying_code, stream=args.stream, history_path=args.history)
            output = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        print(output)
    except (OSError, ValueError, ZoneInfoNotFoundError, TimeoutExpired) as error:
        print(f"گزارش پژوهشی در دسترس نیست: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if "--research" in sys.argv[1:]:
        raise SystemExit(research_main(sys.argv[1:]))
    main()
