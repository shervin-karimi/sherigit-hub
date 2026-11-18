import json
import math
import sys

sys.path.insert(0, ".")

from calculate_greeks import (
    implied_volatility,
    calculate_greeks,
)

INPUT = "data/greeks_results.json"
RATES = [0.30, 0.35, 0.40, 0.4394, 0.50]

with open(INPUT, encoding="utf-8") as f:
    records = json.load(f)

def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

results = []

for r in records:
    S = num(r.get("underlying_closing_price"))
    K = num(r.get("strike_price"))
    T_days = num(r.get("time_to_expiry_days"))
    price = num(r.get("option_mid_price"))
    option_type = r.get("option_type")

    if not all(x is not None for x in [S, K, T_days, price]):
        continue
    if S <= 0 or K <= 0 or T_days <= 0 or price <= 0:
        continue

    T = T_days / 365

    for rate in RATES:
        iv = implied_volatility(
            option_type, price, S, K, T, rate, 0.0
        )

        greeks = calculate_greeks(
            option_type, S, K, T, rate, 0.0, iv
        ) if iv is not None else None

        results.append({
            "symbol": r.get("symbol"),
            "rate": rate,
            "iv": iv,
            "delta": greeks["delta"] if greeks else None,
            "theta": greeks["theta_per_day"] if greeks else None,
            "vega": greeks["vega_per_1pct"] if greeks else None,
        })

print("=" * 55)
print("آزمون حساسیت نرخ بدون ریسک")
print("=" * 55)

for symbol in ["ضستا1065", "ضهرم9030", "طهرم9031"]:
    print("\nقرارداد:", symbol)
    print("نرخ | IV | دلتا | تتا روزانه | وگا")
    for x in results:
        if x["symbol"] == symbol:
            values = [
                f"{x['rate']*100:.2f}%",
                f"{x['iv']*100:.3f}%" if x["iv"] is not None else "ناموجود",
                f"{x['delta']:.5f}" if x["delta"] is not None else "-",
                f"{x['theta']:.5f}" if x["theta"] is not None else "-",
                f"{x['vega']:.5f}" if x["vega"] is not None else "-",
            ]
            print(" | ".join(values))

print("\nتعداد محاسبات:", len(results))
print("این نرخ‌ها فقط سناریوهای آزمایشی هستند.")
