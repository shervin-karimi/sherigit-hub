
import json
from pathlib import Path
from collections import Counter

FILE = Path("data/all_contracts.json")

contracts = json.loads(
    FILE.read_text(encoding="utf-8")
)

def number(value):
    try:
        if value is None:
            return None
        return float(value)
    except (ValueError, TypeError):
        return None

total = len(contracts)

valid_ids = [
    x for x in contracts
    if x.get("ins_code")
]

unique_ids = {
    x["ins_code"] for x in valid_ids
}

active = [
    x for x in contracts
    if (number(x.get("remaining_days")) or 0) > 0
]

traded = [
    x for x in contracts
    if (number(x.get("trade_count")) or 0) > 0
]

volume_positive = [
    x for x in contracts
    if (number(x.get("trade_volume")) or 0) > 0
]

valid_quotes = []
crossed_quotes = []

for x in contracts:
    bid = number(x.get("best_bid"))
    ask = number(x.get("best_ask"))

    if bid is None or ask is None:
        continue

    if bid <= 0 or ask <= 0:
        continue

    if ask < bid:
        crossed_quotes.append(x)
        continue

    mid = (bid + ask) / 2

    spread = ((ask - bid) / mid) * 100

    valid_quotes.append({
        "symbol": x.get("symbol"),
        "type": x.get("option_type"),
        "underlying": x.get("underlying_symbol"),
        "spread_pct": spread,
        "bid": bid,
        "ask": ask,
        "volume": number(x.get("trade_volume")) or 0,
        "trades": number(x.get("trade_count")) or 0
    })

spread_buckets = {
    "0 تا 5 درصد": 0,
    "5 تا 10 درصد": 0,
    "10 تا 20 درصد": 0,
    "بیشتر از 20 درصد": 0
}

for q in valid_quotes:
    s = q["spread_pct"]

    if s <= 5:
        spread_buckets["0 تا 5 درصد"] += 1
    elif s <= 10:
        spread_buckets["5 تا 10 درصد"] += 1
    elif s <= 20:
        spread_buckets["10 تا 20 درصد"] += 1
    else:
        spread_buckets["بیشتر از 20 درصد"] += 1

print("=" * 45)
print("OPTIONS DATA QUALITY REPORT")
print("=" * 45)

print("Total contracts:", total)
print("Contracts with ID:", len(valid_ids))
print("Unique IDs:", len(unique_ids))
print("Duplicate IDs:", len(valid_ids) - len(unique_ids))
print("Not expired:", len(active))
print("Contracts traded:", len(traded))
print("Positive volume:", len(volume_positive))
print("Valid bid/ask:", len(valid_quotes))
print("Crossed quotes:", len(crossed_quotes))

print("\nSpread distribution:")

for name, count in spread_buckets.items():
    print(name, ":", count)

print("\n10 contracts with lowest spread:")

for q in sorted(
    valid_quotes,
    key=lambda x: x["spread_pct"]
)[:10]:

    print(
        q["symbol"],
        q["type"],
        "| Base:", q["underlying"],
        "| Spread:", round(q["spread_pct"], 2), "%",
        "| Volume:", q["volume"],
        "| Trades:", q["trades"]
    )

print("\n10 contracts with highest trading volume:")

for q in sorted(
    contracts,
    key=lambda x: number(x.get("trade_volume")) or 0,
    reverse=True
)[:10]:

    print(
        q["symbol"],
        "|",
        q["option_type"],
        "| Volume:",
        q.get("trade_volume"),
        "| Trades:",
        q.get("trade_count")
    )

Path("reports").mkdir(exist_ok=True)

Path("reports/quality_report.txt").write_text(
    "OPTIONS DATA QUALITY REPORT\n"
    f"Total contracts: {total}\n"
    f"Unique IDs: {len(unique_ids)}\n"
    f"Not expired: {len(active)}\n"
    f"Traded: {len(traded)}\n"
    f"Valid bid/ask: {len(valid_quotes)}\n",
    encoding="utf-8"
)

print("\nReport saved: reports/quality_report.txt")
