
import json

with open("data/enriched_contracts.json", encoding="utf-8") as f:
    data = json.load(f)

# فقط قراردادهایی که از فیلتر نقدشوندگی عبور کرده‌اند
with open("data/filtered_contracts.json", encoding="utf-8") as f:
    filtered = json.load(f)

selected_symbols = {x["symbol"] for x in filtered}
data = [x for x in data if x["symbol"] in selected_symbols]

negative_mid = 0
negative_ask = 0
valid = 0
anomalies = []

for x in data:
    intrinsic = x.get("intrinsic_value")
    bid = x.get("best_bid") or 0
    ask = x.get("best_ask") or 0

    if intrinsic is None or ask <= 0 or bid <= 0:
        continue

    valid += 1

    mid = (bid + ask) / 2
    mid_time_value = mid - intrinsic
    ask_time_value = ask - intrinsic
    bid_time_value = bid - intrinsic

    if mid_time_value < 0:
        negative_mid += 1

    if ask_time_value < 0:
        negative_ask += 1

    if mid_time_value < 0 or ask_time_value < 0:
        anomalies.append({
            "symbol": x["symbol"],
            "type": x["option_type"],
            "base": x["underlying_symbol"],
            "spot": x["underlying_closing_price"],
            "strike": x["strike_price"],
            "intrinsic": intrinsic,
            "bid": bid,
            "ask": ask,
            "mid_time_value": round(mid_time_value, 2),
            "ask_time_value": round(ask_time_value, 2),
            "days": x["remaining_days"]
        })

print("=" * 45)
print("TIME VALUE VALIDATION")
print("=" * 45)
print("Selected contracts:", len(data))
print("Valid quotes:", valid)
print("Negative mid time value:", negative_mid)
print("Negative ask time value:", negative_ask)

print("\nContracts needing investigation:")

for x in anomalies[:20]:
    print(
        x["symbol"],
        "|", x["type"],
        "| Spot:", x["spot"],
        "| Strike:", x["strike"],
        "| Intrinsic:", x["intrinsic"],
        "| Bid:", x["bid"],
        "| Ask:", x["ask"],
        "| Mid TV:", x["mid_time_value"],
        "| Ask TV:", x["ask_time_value"]
    )
