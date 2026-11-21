
import json
import csv
from pathlib import Path

from candidate_rules import evaluate_candidate

with open("data/enriched_contracts.json", encoding="utf-8") as f:
    contracts = json.load(f)

candidates = []

for c in contracts:
    item, _ = evaluate_candidate(c)
    if item is not None:
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
