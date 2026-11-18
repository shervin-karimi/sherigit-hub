import json
from pathlib import Path
from datetime import datetime, timezone

INPUT_FILE = Path("data/underlying_history.json")
OUTPUT_FILE = Path("data/underlying_trends_v2.json")
TEMP_FILE = Path("data/underlying_trends_v2.tmp")

MAX_DATA_AGE_HOURS = 36
MIN_ACTIVE_SESSIONS = 21


def now():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def number(value):
    try:
        result = float(value)
        if result > 0:
            return result
    except (TypeError, ValueError):
        pass
    return None


def average(values):
    return sum(values) / len(values)


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

    return_5 = (latest / price_5 - 1) * 100
    return_20 = (latest / price_20 - 1) * 100

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


def main():
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


if __name__ == "__main__":
    main()
