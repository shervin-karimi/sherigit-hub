import json
from calculate_greeks import implied_volatility, calculate_greeks

INPUT = "data/greeks_results.json"
RATES = [0.30, 0.35, 0.40, 0.4394, 0.50]

with open(INPUT, encoding="utf-8") as f:
    contracts = json.load(f)

def num(x, default=0):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default

def sort_key(x):
    target = 0.60 if x["option_type"] == "CALL" else -0.60
    return (
        num(x.get("spread_percent"), 999),
        -num(x.get("trade_count")),
        -num(x.get("trade_volume")),
        abs(num(x.get("delta")) - target)
    )

rankings = {}

for rate in RATES:
    calls, puts = [], []

    for original in contracts:
        x = dict(original)

        if x.get("option_type") not in ("CALL", "PUT"):
            continue

        if num(x.get("remaining_days")) < 21:
            continue
        if num(x.get("spread_percent"), 999) > 3:
            continue
        if num(x.get("trade_count")) < 30:
            continue
        if num(x.get("trade_volume")) <= 0:
            continue
        if num(x.get("best_bid")) <= 0 or num(x.get("best_ask")) <= 0:
            continue
        if num(x.get("bid_volume")) <= 0 or num(x.get("ask_volume")) <= 0:
            continue
        if num(x.get("ask_time_value"), -1) <= 0:
            continue

        S = num(x.get("underlying_closing_price"))
        K = num(x.get("strike_price"))
        price = num(x.get("option_mid_price"))
        days = num(x.get("time_to_expiry_days"))

        if min(S, K, price, days) <= 0:
            continue

        T = days / 365

        iv = implied_volatility(
            x["option_type"], price, S, K, T, rate, 0.0
        )
        if iv is None:
            continue

        greeks = calculate_greeks(
            x["option_type"], S, K, T, rate, 0.0, iv
        )
        if not greeks:
            continue

        delta = greeks.get("delta")
        if delta is None:
            continue

        if x["option_type"] == "CALL" and not 0.35 <= delta <= 0.80:
            continue
        if x["option_type"] == "PUT" and not -0.80 <= delta <= -0.35:
            continue

        x["delta"] = delta
        x["iv_scenario"] = iv

        if x["option_type"] == "CALL":
            calls.append(x)
        else:
            puts.append(x)

    calls.sort(key=sort_key)
    puts.sort(key=sort_key)

    rankings[rate] = {
        "CALL": [x["symbol"] for x in calls],
        "PUT": [x["symbol"] for x in puts]
    }

    print(f"\nنرخ بدون ریسک: {rate*100:.2f}%")
    print(f"تعداد Call واجد شرایط: {len(calls)}")
    print(f"تعداد Put واجد شرایط: {len(puts)}")
    print("۱۰ Call برتر:", "، ".join(x["symbol"] for x in calls[:10]))
    print("۱۰ Put برتر:", "، ".join(x["symbol"] for x in puts[:10]))

print("\n" + "=" * 45)
print("پایداری حضور در ۱۰ رتبه اول")
print("=" * 45)

for option_type in ("CALL", "PUT"):
    counts = {}
    for result in rankings.values():
        for symbol in result[option_type][:10]:
            counts[symbol] = counts.get(symbol, 0) + 1

    print("\n", "اختیار خرید" if option_type == "CALL" else "اختیار فروش")
    for symbol, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"{symbol}: از ۵ سناریو در {count} سناریو")

print("\nاین آزمون سناریویی است، نه سیگنال خرید یا فروش.")
