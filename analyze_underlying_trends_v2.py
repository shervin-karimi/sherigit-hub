import argparse
import json
import math
import re
import sys
from pathlib import Path
from datetime import date, datetime, timezone
from statistics import mean, stdev
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

INPUT_FILE = Path("data/underlying_history.json")
OUTPUT_FILE = Path("data/underlying_trends_v2.json")
TEMP_FILE = Path("data/underlying_trends_v2.tmp")

MAX_DATA_AGE_HOURS = 36
MIN_ACTIVE_SESSIONS = 21
ANNUALIZATION_SESSIONS = 252  # Illustrative convention; not an estimate of the TSE calendar.
MACD_WARMUP = 60  # Diagnostic only; first full MACD value is defined at 34 closes.


def now():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def finite_number(value):
    try:
        if isinstance(value, bool):
            return None
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def number(value):
    result = finite_number(value)
    return result if result is not None and result > 0 else None


def average(values):
    return mean(values)


def percent_return(latest, previous):
    return (latest / previous - 1) * 100


def invalid_result(name, reason):
    return {
        "symbol": name,
        "trend": "نامشخص",
        "confidence": "نامعتبر",
        "analysis_status": "rejected",
        "reason": reason,
    }


def analyze(name, item, metadata):
    if not isinstance(item, dict):
        return invalid_result(name, "ساختار تاریخچه نامعتبر است")

    fetched_at = parse_time(item.get("fetched_at"))
    current_time = now()

    if fetched_at is None:
        return invalid_result(name, "زمان دریافت تاریخچه معتبر نیست")

    age_hours = (current_time - fetched_at).total_seconds() / 3600

    if age_hours < 0 or age_hours > MAX_DATA_AGE_HOURS:
        return invalid_result(name, "تاریخچه قدیمی است")

    rows = item.get("history")

    if not isinstance(rows, list):
        return invalid_result(name, "فهرست تاریخچه وجود ندارد")

    expected_code = str(item.get("code", "")).strip()

    if not expected_code:
        return invalid_result(name, "کد نماد موجود نیست")

    seen_dates = set()
    active = []

    for row in rows:
        if not isinstance(row, dict):
            return invalid_result(name, "رکورد نامعتبر در تاریخچه")

        try:
            date_number = int(row.get("dEven"))
            datetime.strptime(str(date_number), "%Y%m%d")
        except (TypeError, ValueError):
            return invalid_result(name, "تاریخ نامعتبر در تاریخچه")

        if date_number in seen_dates:
            return invalid_result(name, "تاریخ تکراری در تاریخچه")

        seen_dates.add(date_number)

        if str(row.get("insCode", "")) != expected_code:
            return invalid_result(name, "کد نماد با رکورد تاریخچه تطبیق ندارد")

        quality = row.get("_quality", {})

        close = number(row.get("pClosing"))
        volume = number(row.get("qTotTran5J"))

        if (
            close is not None
            and volume is not None
            and quality.get("close_valid") is True
            and quality.get("volume_valid") is True
        ):
            active.append({
                "date": date_number,
                "close": close,
            })

    active.sort(key=lambda x: x["date"], reverse=True)

    if len(active) < MIN_ACTIVE_SESSIONS:
        return invalid_result(
            name,
            f"جلسات معتبر ناکافی: {len(active)}"
        )

    # برای محاسبه بازده ۵ و ۲۰ جلسه، قیمت مبنا در اندیس‌های ۵ و ۲۰ است.
    recent = active[:21]
    closes = [row["close"] for row in recent]

    latest = closes[0]
    price_5 = closes[5]
    price_20 = closes[20]

    return_5 = percent_return(latest, price_5)
    return_20 = percent_return(latest, price_20)

    ma5 = average(closes[:5])
    ma20 = average(closes[:20])

    distance_ma20 = (latest / ma20 - 1) * 100
    acceleration = return_5 - return_20 / 4

    score = 0

    if return_5 > 0:
        score += 25

    if return_20 > 0:
        score += 25

    if latest > ma5:
        score += 25

    if ma5 > ma20:
        score += 25

    # قواعد جهت روند؛ امتیاز به‌تنهایی کافی نیست.
    if (
        score >= 75
        and return_20 > 0
        and latest > ma20
        and ma5 > ma20
    ):
        trend = "صعودی"

    elif (
        score <= 25
        and return_20 < 0
        and latest < ma20
        and ma5 < ma20
    ):
        trend = "نزولی"

    else:
        trend = "خنثی"

    if trend == "صعودی" and acceleration > 0:
        confidence = "متوسط"
    elif trend == "نزولی" and acceleration < 0:
        confidence = "متوسط"
    elif trend == "خنثی":
        confidence = "پایین"
    else:
        confidence = "پایین"

    if acceleration > 1:
        momentum = "شتاب مثبت"
    elif acceleration < -1:
        momentum = "شتاب منفی"
    else:
        momentum = "شتاب کم یا خنثی"

    return {
        "symbol": name,
        "latest_date": recent[0]["date"],
        "latest_price": latest,
        "active_sessions": len(active),
        "history_age_hours": round(age_hours, 2),
        "return_5_sessions": round(return_5, 2),
        "return_20_sessions": round(return_20, 2),
        "moving_average_5": round(ma5, 2),
        "moving_average_20": round(ma20, 2),
        "distance_from_ma20_percent": round(distance_ma20, 2),
        "acceleration": round(acceleration, 2),
        "momentum": momentum,
        "direction_score": score,
        "trend": trend,
        "confidence": confidence,
        "analysis_status": "ok",
        "reason": "تحلیل اولیه بر اساس قیمت پایانی و حجم معتبر",
        "model_version": "2.1-experimental",
    }


def legacy_main():
    with open(INPUT_FILE, encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("ساختار فایل تاریخچه نامعتبر است")

    symbols = data.get("symbols")

    if not isinstance(symbols, dict) or not symbols:
        raise ValueError("بخش symbols در تاریخچه وجود ندارد")

    metadata = data.get("metadata", {})
    results = []

    print("=" * 55)
    print("تحلیل روند دارایی‌های پایه - نسخه آزمایشی 2.1")
    print("=" * 55)

    for name, item in sorted(symbols.items()):
        result = analyze(name, item, metadata)
        results.append(result)

        print(f"\nنماد: {name}")
        print("وضعیت:", result["analysis_status"])
        print("روند:", result["trend"])
        print("قدرت اطمینان:", result["confidence"])
        print("دلیل:", result["reason"])

        if result["analysis_status"] == "ok":
            print("بازده ۵ جلسه:", result["return_5_sessions"])
            print("بازده ۲۰ جلسه:", result["return_20_sessions"])
            print("امتیاز جهت:", result["direction_score"], "از ۱۰۰")
            print("شتاب:", result["momentum"])

    output = {
        "metadata": {
            "generated_at": now().isoformat(),
            "source": str(INPUT_FILE),
            "model_version": "2.1-experimental",
            "symbol_count": len(results),
            "warning": "این خروجی سیگنال قطعی خرید یا فروش نیست.",
        },
        "results": results,
    }

    with open(TEMP_FILE, "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)
        file.flush()

    TEMP_FILE.replace(OUTPUT_FILE)

    print("\nتحلیل تمام شد.")
    print("تعداد نمادها:", len(results))
    print("فایل خروجی:", OUTPUT_FILE)


def session_date(value):
    if type(value) not in (int, str) or not re.fullmatch(r"[0-9]{8}", str(value)):
        raise ValueError("تاریخ روزانه باید YYYYMMDD باشد.")
    return datetime.strptime(str(value), "%Y%m%d").date()


def ema_series(values, period):
    if len(values) < period:
        return []
    result = [average(values[:period])]
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result.append(result[-1] + alpha * (value - result[-1]))
    return result


def wilder_rsi(values, period=14):
    changes = [b - a for a, b in zip(values, values[1:])]
    gains = average([max(0, x) for x in changes[:period]])
    losses = average([max(0, -x) for x in changes[:period]])
    for change in changes[period:]:
        gains += (max(0, change) - gains) / period
        losses += (max(0, -change) - losses) / period
    if losses == 0:
        return 50.0 if gains == 0 else 100.0
    return 100 - 100 / (1 + gains / losses)


def macd(values):
    fast, slow = ema_series(values, 12), ema_series(values, 26)
    line = [a - b for a, b in zip(fast[26 - 12:], slow)]
    signal = ema_series(line, 9)
    return {"line": line[-1], "signal": signal[-1], "histogram": line[-1] - signal[-1]}


def historical_volatility(values):
    logs = [math.log(price) for price in values[-21:]]
    return stdev([b - a for a, b in zip(logs, logs[1:])]) * math.sqrt(ANNUALIZATION_SESSIONS) * 100


def computed(value):
    numbers = value.values() if isinstance(value, dict) else (value,)
    if not all(math.isfinite(n) for n in numbers):
        return unavailable("NUMERIC_INVALID", "نتیجهٔ عددی متناهی نیست.")
    return {"status": "COMPUTED", "value": value}


def unavailable(status, reason):
    return {"status": status, "reason": reason}


def observed_indicators(rows):
    closes = [row["close"] for row in rows]

    def calculate(count, fn):
        if len(closes) < count:
            return unavailable("INSUFFICIENT_HISTORY", f"{count} قیمت پایانی لازم؛ {len(closes)} موجود.")
        return computed(fn())

    indicators = {
        "SMA(5)": calculate(5, lambda: average(closes[-5:])),
        "SMA(20)": calculate(20, lambda: average(closes[-20:])),
        "RETURN(5)_PCT": calculate(6, lambda: percent_return(closes[-1], closes[-6])),
        "RETURN(20)_PCT": calculate(21, lambda: percent_return(closes[-1], closes[-21])),
        "RSI(14)_WILDER": calculate(15, lambda: wilder_rsi(closes)),
        "MACD(12,26,9)": calculate(34, lambda: macd(closes)),
        "HV(20)_ANNUALIZED_PCT_SQRT252": calculate(21, lambda: historical_volatility(closes)),
    }
    if indicators["MACD(12,26,9)"]["status"] == "COMPUTED":
        indicators["MACD(12,26,9)"]["warmup"] = "LIMITED" if len(closes) < MACD_WARMUP else "AT_LEAST_60_CLOSES"
    window = rows[-20:]
    for key, required, fields, fn in (
        ("VOLUME(20)", ("volume",), "qTotTran5J", lambda: {
            "latest": window[-1]["volume"], "mean": average([row["volume"] for row in window]),
        }),
        ("RANGE(20)", ("high", "low"), "priceMax/priceMin", lambda: {
            "rolling_high": max(row["high"] for row in window), "rolling_low": min(row["low"] for row in window),
        }),
    ):
        if not window or any(row[field] is None for row in window for field in required):
            indicators[key] = unavailable("NOT_AVAILABLE", f"دادهٔ معتبر و کامل {fields} در پنجره موجود نیست.")
        else:
            indicators[key] = calculate(20, fn)
    return indicators


def research_report(name, item, metadata, *, before, calendar=None, expected_code=None):
    """Retrospective observed bars only; `before` is an exclusive Tehran session date."""
    if not isinstance(item, dict) or not isinstance(metadata, dict) or not isinstance(item.get("history"), list):
        raise ValueError("ساختار تاریخچهٔ روزانه نامعتبر است.")
    code = item.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[1-9][0-9]*", code) or (
        expected_code is not None and code != expected_code
    ):
        raise ValueError("شناسهٔ دارایی پایه نامعتبر یا متعارض است.")
    received = parse_time(item.get("fetched_at"))
    if received is None:
        raise ValueError("زمان محلی دریافت با منطقهٔ زمانی لازم است.")
    # Exclude both the requested day and any bar from the download day (which may be incomplete).
    cutoff = min(before, received.astimezone(ZoneInfo("Asia/Tehran")).date())
    seen, rows, zero_volume, excluded = set(), [], [], 0
    for raw in item["history"]:
        if not isinstance(raw, dict):
            raise ValueError("رکورد تاریخچه نامعتبر است.")
        day = session_date(raw.get("dEven"))
        if day >= cutoff:
            excluded += 1
            continue
        if day in seen:
            raise ValueError(f"تاریخ تکراری: {day}")
        seen.add(day)
        if raw.get("insCode") != code:
            raise ValueError(f"شناسهٔ رکورد با دارایی پایه تطابق ندارد: {day}")
        close = number(raw.get("pClosing"))
        if close is None:
            raise ValueError(f"قیمت پایانی نامعتبر: {day}")
        raw_volume = finite_number(raw.get("qTotTran5J"))
        if raw_volume == 0:
            zero_volume.append(day)
            continue
        volume = raw_volume if raw_volume is not None and raw_volume > 0 else None
        low, high = number(raw.get("priceMin")), number(raw.get("priceMax"))
        if low is None or high is None or not low <= close <= high:
            low = high = None
        rows.append({"date": day, "close": close, "volume": volume, "low": low, "high": high})
    rows.sort(key=lambda row: row["date"])
    sessions = {date.fromisoformat(value) for value in calendar["sessions"]} if calendar else set()
    calendar_range = (min(sessions), max(sessions)) if sessions else None
    first_day = min(seen) if seen else cutoff
    missing = {day for day in sessions - seen if first_day <= day < cutoff}
    outside = (
        {day for day in seen - sessions if calendar_range[0] <= day <= calendar_range[1]}
        if calendar_range else set()
    )
    effective_dates = [row["date"] for row in rows]
    date_range = lambda days: [min(days).isoformat(), max(days).isoformat()] if days else None
    return {
        "symbol": name, "ins_code": code,
        "source": metadata.get("source") or "UNKNOWN",
        "downloaded_at_utc": received.isoformat(), "session_date_cutoff_exclusive": cutoff.isoformat(),
        "dataset_date_range": date_range(seen), "valid_close_date_range": date_range(effective_dates),
        "records_before_cutoff": len(seen), "valid_close_count": len(rows), "excluded_at_or_after_cutoff": excluded,
        "volume_present_count": sum(row["volume"] is not None for row in rows),
        "high_low_present_count": sum(row["low"] is not None for row in rows),
        "zero_volume_excluded": sorted(day.isoformat() for day in zero_volume),
        "missing_listed_sessions": sorted(day.isoformat() for day in missing) if calendar else None,
        "observations_outside_listed_sessions": sorted(day.isoformat() for day in outside) if calendar else None,
        "session_calendar": calendar["source"] if calendar else None,
        "session_calendar_date_range": list(map(date.isoformat, calendar_range)) if calendar_range else None,
        "source_status": "UNVERIFIED", "adjustment_status": "UNKNOWN", "price_and_volume_units": "UNVERIFIED",
        "session_completeness": "OPERATOR_CALENDAR_UNVERIFIED" if calendar else "UNKNOWN",
        "historical_availability": "NOT_VERIFIED_NO_POINT_IN_TIME_BACKTEST",
        "sample_basis": "observed_valid_daily_closes_not_assumed_consecutive_exchange_sessions",
        "status": "RESEARCH_ONLY", "execution_eligible": False,
        "greeks_assumption_status": "experimental_unverified",
        "notice": "زمان دریافت زمان کندل یا مظنه نیست؛ اعداد پژوهشی‌اند، نه سیگنال یا مجوز معامله.",
        "indicator_notes": {
            "HV": "سالانه‌سازی فرضی با ۲۵۲ مشاهده؛ تعداد جلسات بورس و پیوستگی بازده‌ها تأیید نشده است.",
            "RANGE": "سقف و کف مشاهده‌شده در پنجره؛ سطح پیش‌بینی‌شدهٔ حمایت یا مقاومت نیست.",
        },
        "indicators": observed_indicators(rows),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="گزارش پژوهشی آفلاین تاریخچهٔ روزانهٔ پایه")
    parser.add_argument("--input", type=Path, default=INPUT_FILE)
    parser.add_argument("--symbol", default="خودرو")
    parser.add_argument("--ins-code", help="شناسهٔ مورد انتظار؛ برای خودرو مقدار مستند محلی پیش‌فرض است")
    parser.add_argument("--before", type=date.fromisoformat, help="تاریخ میلادی انحصاری جلسهٔ تهران")
    parser.add_argument("--sessions", type=Path, help="فهرست محلی جلسات به شکل shadow_multiday")
    parser.add_argument("--legacy-output", action="store_true", help="فقط مسیر قدیمی تولید فایل ترکیب تحلیل")
    args = parser.parse_args(argv)
    if args.legacy_output:
        return legacy_main()
    try:
        with args.input.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), dict):
            raise ValueError("ساختار فایل تاریخچه نامعتبر است.")
        if args.symbol not in data["symbols"]:
            raise ValueError("نماد در تاریخچه موجود نیست.")
        calendar = None
        if args.sessions is not None:
            from shadow_multiday import session_calendar
            calendar = session_calendar(args.sessions)
        local_today = now().astimezone(ZoneInfo("Asia/Tehran")).date()
        report = research_report(
            args.symbol, data["symbols"][args.symbol], data.get("metadata", {}),
            before=min(local_today, args.before or local_today), calendar=calendar,
            expected_code=args.ins_code or ("65883838195688438" if args.symbol == "خودرو" else None),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, ZoneInfoNotFoundError) as error:
        print(f"گزارش پژوهشی در دسترس نیست: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
