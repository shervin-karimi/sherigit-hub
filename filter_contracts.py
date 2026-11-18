
import json
import csv
from pathlib import Path

INPUT = Path("data/all_contracts.json")
OUTPUT_JSON = Path("data/filtered_contracts.json")
OUTPUT_CSV = Path("data/filtered_contracts.csv")

MAX_SPREAD_PERCENT = 5
MIN_REMAINING_DAYS = 14

with INPUT.open("r", encoding="utf-8") as f:
    contracts = json.load(f)

filtered = []
reasons = {
    "invalid_quotes": 0,
    "wide_spread": 0,
    "no_trades": 0,
    "no_volume": 0,
    "low_order_depth": 0,
    "near_expiry": 0,
}

for c in contracts:
    bid = c.get("best_bid") or 0
    ask = c.get("best_ask") or 0

    if bid <= 0 or ask <= 0 or ask < bid:
        reasons["invalid_quotes"] += 1
        continue

    mid = (bid + ask) / 2
    spread = ((ask - bid) / mid) * 100

    if spread > MAX_SPREAD_PERCENT:
        reasons["wide_spread"] += 1
        continue

    if (c.get("trade_count") or 0) <= 0:
        reasons["no_trades"] += 1
        continue

    if (c.get("trade_volume") or 0) <= 0:
        reasons["no_volume"] += 1
        continue

    if (c.get("bid_volume") or 0) <= 0 or (c.get("ask_volume") or 0) <= 0:
        reasons["low_order_depth"] += 1
        continue

    if (c.get("remaining_days") or 0) < MIN_REMAINING_DAYS:
        reasons["near_expiry"] += 1
        continue

    item = dict(c)
    item["spread_percent"] = round(spread, 3)
    filtered.append(item)

# مرتب‌سازی: ابتدا اسپرد کمتر، سپس حجم بیشتر
filtered.sort(
    key=lambda x: (
        x["spread_percent"],
        -(x.get("trade_volume") or 0)
    )
)

OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)

with OUTPUT_JSON.open("w", encoding="utf-8") as f:
    json.dump(filtered, f, ensure_ascii=False, indent=2)

if filtered:
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=filtered[0].keys()
        )
        writer.writeheader()
        writer.writerows(filtered)

print("=" * 45)
print("FILTER RESULTS")
print("=" * 45)
print("Total contracts:", len(contracts))
print("Selected contracts:", len(filtered))
print("Rejected contracts:", len(contracts) - len(filtered))

print("\nRejection counts by first failed condition:")
for name, count in reasons.items():
    print(name, ":", count)

print("\nTop 20 selected contracts:")
for c in filtered[:20]:
    print(
        c["symbol"],
        "|", c["option_type"],
        "| Base:", c["underlying_symbol"],
        "| Spread:", c["spread_percent"], "%",
        "| Volume:", c["trade_volume"],
        "| Trades:", c["trade_count"],
        "| Days:", c["remaining_days"]
    )

print("\nSaved:")
print(OUTPUT_JSON)
print(OUTPUT_CSV)
