
import json
from collections import Counter, defaultdict
from statistics import median

with open("data/filtered_contracts.json", encoding="utf-8") as f:
    data = json.load(f)

print("=" * 45)
print("FILTERED CONTRACT ANALYSIS")
print("=" * 45)

print("Total:", len(data))

# تفکیک CALL و PUT
types = Counter(x["option_type"] for x in data)
print("\nCALL / PUT:")
for name, count in types.items():
    print(name, ":", count)

# تعداد قراردادها به تفکیک دارایی پایه
underlyings = Counter(x["underlying_symbol"] for x in data)

print("\nContracts by underlying:")
for name, count in underlyings.most_common():
    print(name, ":", count)

# بررسی تعداد معاملات
trades = [x.get("trade_count", 0) or 0 for x in data]
volumes = [x.get("trade_volume", 0) or 0 for x in data]

print("\nTrading statistics:")
print("Median trades:", median(trades))
print("Median volume:", median(volumes))
print("Contracts with fewer than 10 trades:",
      sum(1 for x in trades if x < 10))
print("Contracts with fewer than 30 trades:",
      sum(1 for x in trades if x < 30))
print("Contracts with at least 100 trades:",
      sum(1 for x in trades if x >= 100))

# کم‌معامله‌ترین قراردادها
print("\n20 contracts with lowest trade count:")
for x in sorted(data, key=lambda x: x.get("trade_count", 0))[:20]:
    print(
        x["symbol"],
        "|", x["option_type"],
        "| Base:", x["underlying_symbol"],
        "| Trades:", x["trade_count"],
        "| Volume:", x["trade_volume"],
        "| Spread:", x["spread_percent"],
        "| Days:", x["remaining_days"]
    )

# توزیع CALL و PUT برای هر دارایی پایه
print("\nCALL / PUT by underlying:")
groups = defaultdict(Counter)

for x in data:
    groups[x["underlying_symbol"]][x["option_type"]] += 1

for name in sorted(groups):
    print(
        name,
        "| CALL:", groups[name]["CALL"],
        "| PUT:", groups[name]["PUT"]
    )
