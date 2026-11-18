import json
import math
import csv
from pathlib import Path
from collections import Counter

INPUT = Path("data/greeks_results.json")
OUTPUT_JSON = Path("data/greeks_quality.json")
OUTPUT_CSV = Path("data/greeks_quality.csv")

with INPUT.open(encoding="utf-8") as f:
    records = json.load(f)

def number(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

checked = []
issues_count = Counter()

for r in records:
    issues = []

    iv = number(r.get("iv_mid"))
    delta = number(r.get("delta"))
    gamma = number(r.get("gamma"))
    theta = number(r.get("theta_per_day"))
    vega = number(r.get("vega_per_1pct"))

    if iv is None:
        issues.append("فاقد IV میانی")
    else:
        if iv < 5:
            issues.append("IV کمتر از ۵٪")
        elif iv > 150:
            issues.append("IV بالاتر از ۱۵۰٪")

    option_type = r.get("option_type")
    if delta is None:
        issues.append("دلتا محاسبه نشده")
    elif option_type == "CALL" and not (0 <= delta <= 1):
        issues.append("دلتا خارج از دامنه CALL")
    elif option_type == "PUT" and not (-1 <= delta <= 0):
        issues.append("دلتا خارج از دامنه PUT")

    if gamma is None:
        issues.append("گاما محاسبه نشده")
    elif gamma < 0:
        issues.append("گامای منفی")

    if theta is None:
        issues.append("تتا محاسبه نشده")
    elif theta > 0:
        issues.append("تتای مثبت")

    if vega is None:
        issues.append("وگا محاسبه نشده")
    elif vega < 0:
        issues.append("وگای منفی")

    status = r.get("iv_status", "")
    if status and status != "ok":
        issues.append("وضعیت IV: " + str(status))

    assumption_status = r.get("greeks_assumption_status", "missing")
    if assumption_status != "validated":
        issues.append("فرض‌های Greeks تأیید نشده‌اند: " + str(assumption_status))

    for issue in set(issues):
        issues_count[issue] += 1

    checked.append({
        "symbol": r.get("symbol"),
        "option_type": option_type,
        "underlying_symbol": r.get("underlying_symbol"),
        "iv_mid": iv,
        "delta": delta,
        "gamma": gamma,
        "theta_per_day": theta,
        "vega_per_1pct": vega,
        "spread_percent": number(r.get("spread_percent")),
        "trade_count": number(r.get("trade_count")),
        "iv_status": status,
        "greeks_assumption_status": assumption_status,
        "quality_status": "نیازمند بررسی" if issues else "عبور از کنترل اولیه",
        "issues": "؛ ".join(issues)
    })

with OUTPUT_JSON.open("w", encoding="utf-8") as f:
    json.dump(checked, f, ensure_ascii=False, indent=2)

if checked:
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=checked[0].keys())
        writer.writeheader()
        writer.writerows(checked)

passed = sum(x["quality_status"] == "عبور از کنترل اولیه" for x in checked)
flagged = len(checked) - passed

print("=" * 45)
print("گزارش کنترل کیفیت یونانی‌ها")
print("=" * 45)
print(f"کل قراردادها: {len(checked)}")
print(f"عبور از کنترل اولیه: {passed}")
print(f"نیازمند بررسی: {flagged}")
print("\nموارد علامت‌گذاری‌شده:")
for issue, count in issues_count.most_common():
    print(f"{issue}: {count}")

print("\nنمونه قراردادهای نیازمند بررسی:")
shown = 0
for r in checked:
    if r["issues"]:
        print(f'{r["symbol"]} | {r["quality_status"]} | {r["issues"]}')
        shown += 1
        if shown >= 15:
            break

print("\nفایل‌ها ذخیره شدند:")
print(OUTPUT_JSON)
print(OUTPUT_CSV)
