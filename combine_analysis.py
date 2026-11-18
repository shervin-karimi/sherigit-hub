import json
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).parent
RANKED_FILE = BASE / "data" / "ranked_candidates.json"
TRENDS_FILE = BASE / "data" / "underlying_trends_v2.json"

OUTPUT_JSON = BASE / "data" / "combined_analysis.json"
OUTPUT_TXT = BASE / "data" / "گزارش_تطبیقی.txt"

MAX_AGE_HOURS = 36
EXPECTED_MODEL = "2.1-experimental"


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


if __name__ == "__main__":
    main()
