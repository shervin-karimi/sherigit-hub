import json
from pathlib import Path

INPUT_FILE = Path("data/underlying_history.json")
OUTPUT_FILE = Path("data/underlying_trends.json")


def calculate_average(values):
    return sum(values) / len(values)


def analyze(name, item):
    rows = item["history"]

    if len(rows) < 21:
        return {
            "symbol": name,
            "status": "اطلاعات ناکافی",
        }

    closes = [
        float(row["pClosing"])
        for row in rows
    ]

    latest = closes[0]
    price_5_ago = closes[5]
    price_20_ago = closes[20]

    return_5 = (
        (latest / price_5_ago) - 1
    ) * 100

    return_20 = (
        (latest / price_20_ago) - 1
    ) * 100

    ma5 = calculate_average(closes[:5])
    ma20 = calculate_average(closes[:20])

    if (
        latest > ma5 > ma20
        and return_5 > 0
        and return_20 > 0
    ):
        trend = "صعودی"

    elif (
        latest < ma5 < ma20
        and return_5 < 0
        and return_20 < 0
    ):
        trend = "نزولی"

    else:
        trend = "خنثی"

    return {
        "symbol": name,
        "latest_date": rows[0]["dEven"],
        "latest_price": latest,
        "return_5_sessions": round(return_5, 2),
        "return_20_sessions": round(return_20, 2),
        "moving_average_5": round(ma5, 2),
        "moving_average_20": round(ma20, 2),
        "trend": trend,
    }


def main():
    with open(INPUT_FILE, encoding="utf-8") as file:
        data = json.load(file)

    results = []

    print("=" * 55)
    print("تحلیل روند نمادهای پایه")
    print("=" * 55)

    for name, item in data.items():
        result = analyze(name, item)
        results.append(result)

        if result["status"] == "اطلاعات ناکافی" if "status" in result else False:
            print(f"\n{name}: اطلاعات ناکافی")
            continue

        print(f"\nنماد: {name}")
        print("بازده ۵ جلسه:", result["return_5_sessions"], "درصد")
        print("بازده ۲۰ جلسه:", result["return_20_sessions"], "درصد")
        print("میانگین ۵ روزه:", result["moving_average_5"])
        print("میانگین ۲۰ روزه:", result["moving_average_20"])
        print("وضعیت روند:", result["trend"])

    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
        json.dump(
            results,
            file,
            ensure_ascii=False,
            indent=2
        )

    print("\n" + "=" * 55)
    print("تحلیل تمام شد.")
    print("فایل ذخیره‌شده:", OUTPUT_FILE)


if __name__ == "__main__":
    main()
