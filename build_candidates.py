
import json
import csv
from pathlib import Path

with open("data/enriched_contracts.json", encoding="utf-8") as f:
    contracts = json.load(f)

MIN_TRADES = 30
MAX_SPREAD = 5
MIN_DAYS = 21

candidates = []

for c in contracts:
    bid = c.get("best_bid") or 0
    ask = c.get("best_ask") or 0
    spot = c.get("underlying_closing_price") or 0
    intrinsic = c.get("intrinsic_value")

    if bid <= 0 or ask < bid or spot <= 0:
        continue

    if (c.get("trade_count") or 0) < MIN_TRADES:
        continue

    if (c.get("trade_volume") or 0) <= 0:
        continue

    if (c.get("bid_volume") or 0) <= 0:
        continue

    if (c.get("ask_volume") or 0) <= 0:
        continue

    if (c.get("remaining_days") or 0) < MIN_DAYS:
        continue

    mid = (bid + ask) / 2
    spread = (ask - bid) / mid * 100

    if spread > MAX_SPREAD:
        continue

    # ارزش زمانی از دید خریدار
    ask_time_value = ask - intrinsic

    if ask_time_value < 0:
        continue

    item = dict(c)
    item["spread_percent"] = round(spread, 3)
    item["ask_time_value"] = round(ask_time_value, 2)
    item["ask_premium_percent_of_spot"] = round(
        ask / spot * 100, 3
    )

    candidates.append(item)

# ابتدا نقدشوندگی بیشتر، سپس اسپرد کمتر
candidates.sort(
    key=lambda x: (
        -(x.get("trade_count") or 0),
        x["spread_percent"]
    )
)

Path("data").mkdir(exist_ok=True)

with open("data/candidates.json", "w", encoding="utf-8") as f:
    json.dump(candidates, f, ensure_ascii=False, indent=2)

if candidates:
    with open("data/candidates.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=candidates[0].keys())
        writer.writeheader()
        writer.writerows(candidates)

print("=" * 45)
print("گزارش قراردادهای منتخب")
print("=" * 45)
print("تعداد قراردادهای منتخب:", len(candidates))

print("\nاختیار خرید:", sum(1 for x in candidates if x["option_type"] == "CALL"))
print("اختیار فروش:", sum(1 for x in candidates if x["option_type"] == "PUT"))

print("\nتعداد قراردادها به تفکیک دارایی پایه:")
from collections import Counter
for name, count in Counter(
    x["underlying_symbol"] for x in candidates
).most_common():
    print(name, ":", count)

print("\n۲۰ قرارداد برتر بر اساس تعداد معاملات:")
for x in candidates[:20]:
    print(
        x["symbol"],
        "|", x["option_type"],
        "| دارایی پایه:", x["underlying_symbol"],
        "| تعداد معاملات:", x["trade_count"],
        "| فاصله خرید و فروش:", x["spread_percent"], "%",
        "| قیمت فروش:", x["best_ask"],
        "| ارزش زمانی:", x["ask_time_value"],
        "| روز تا سررسید:", x["remaining_days"]
    )

print("\nSaved: data/candidates.json")
print("ذخیره شد: data/candidates.csv")
