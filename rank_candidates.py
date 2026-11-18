
import json
import os

INPUT_FILE = "data/greeks_results.json"
OUTPUT_FILE = "data/ranked_candidates.json"

MIN_DAYS = 21
MAX_SPREAD = 3.0
MIN_TRADES = 30

CALL_DELTA_MIN = 0.35
CALL_DELTA_MAX = 0.80

PUT_DELTA_MIN = -0.80
PUT_DELTA_MAX = -0.35


def number(record, field, default=0):
    try:
        value = record.get(field)
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def check_contract(item):
    option_type = item.get("option_type")
    delta = number(item, "delta")

    if option_type not in ("CALL", "PUT"):
        return False, "نوع قرارداد نامعتبر"

    if number(item, "remaining_days") < MIN_DAYS:
        return False, "فاصله تا سررسید کمتر از حد مجاز"

    if number(item, "spread_percent", 999) > MAX_SPREAD:
        return False, "فاصله قیمت خرید و فروش زیاد"

    if number(item, "trade_count") < MIN_TRADES:
        return False, "تعداد معاملات کافی نیست"

    if number(item, "trade_volume") <= 0:
        return False, "حجم معاملات صفر است"

    if number(item, "best_bid") <= 0 or number(item, "best_ask") <= 0:
        return False, "قیمت خرید یا فروش معتبر نیست"

    if number(item, "bid_volume") <= 0 or number(item, "ask_volume") <= 0:
        return False, "عمق سفارش کافی نیست"

    if number(item, "ask_time_value", -1) <= 0:
        return False, "ارزش زمانی در قیمت فروش مثبت نیست"

    if item.get("iv_status") != "ok":
        return False, "محاسبه نوسان ضمنی معتبر نیست"

    if item.get("greeks_assumption_status") != "validated":
        return False, "فرض‌های Greeks هنوز تأیید نشده‌اند"

    if option_type == "CALL":
        if not CALL_DELTA_MIN <= delta <= CALL_DELTA_MAX:
            return False, "دلتا خارج از محدوده اختیار خرید"

    if option_type == "PUT":
        if not PUT_DELTA_MIN <= delta <= PUT_DELTA_MAX:
            return False, "دلتا خارج از محدوده اختیار فروش"

    return True, "واجد شرایط اولیه"


def sort_key(item):
    option_type = item["option_type"]
    delta = number(item, "delta")

    target_delta = 0.60 if option_type == "CALL" else -0.60

    return (
        number(item, "spread_percent"),
        -number(item, "trade_count"),
        -number(item, "trade_volume"),
        abs(delta - target_delta)
    )


def main():
    if not os.path.exists(INPUT_FILE):
        print("خطا: فایل اطلاعات یونانی‌ها پیدا نشد.")
        return

    with open(INPUT_FILE, encoding="utf-8") as f:
        contracts = json.load(f)

    calls = []
    puts = []
    rejected = 0

    for item in contracts:
        valid, reason = check_contract(item)

        if not valid:
            rejected += 1
            continue

        if item["option_type"] == "CALL":
            calls.append(item)
        else:
            puts.append(item)

    calls.sort(key=sort_key)
    puts.sort(key=sort_key)

    result = []

    for option_type, items in (("CALL", calls), ("PUT", puts)):
        for rank, item in enumerate(items, start=1):
            record = dict(item)
            record["rank"] = rank
            result.append(record)

    os.makedirs("data", exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("رتبه‌بندی قراردادها انجام شد.")
    print("--------------------------------")
    print(f"کل قراردادهای بررسی‌شده: {len(contracts)}")
    print(f"قراردادهای واجد شرایط: {len(result)}")
    print(f"قراردادهای کنار گذاشته‌شده: {rejected}")
    print(f"اختیار خرید واجد شرایط: {len(calls)}")
    print(f"اختیار فروش واجد شرایط: {len(puts)}")

    print("\nبرترین اختیارهای خرید:")
    for rank, item in enumerate(calls[:10], start=1):
        print(
            f"{rank}. {item['symbol']} | "
            f"دلتا: {item['delta']} | "
            f"فاصله قیمت: {item['spread_percent']}٪ | "
            f"تعداد معاملات: {item['trade_count']}"
        )

    print("\nبرترین اختیارهای فروش:")
    for rank, item in enumerate(puts[:10], start=1):
        print(
            f"{rank}. {item['symbol']} | "
            f"دلتا: {item['delta']} | "
            f"فاصله قیمت: {item['spread_percent']}٪ | "
            f"تعداد معاملات: {item['trade_count']}"
        )

    print("\nفایل خروجی:", OUTPUT_FILE)
    print("توجه: این رتبه‌بندی به‌تنهایی سیگنال خرید نیست.")
    print("وضعیت روند بازار هنوز باید جداگانه بررسی شود.")


if __name__ == "__main__":
    main()
