import json
import math
import csv
from pathlib import Path
from collections import Counter

INPUT = Path("data/greeks_results.json")
OUTPUT_JSON = Path("data/greeks_quality.json")
OUTPUT_CSV = Path("data/greeks_quality.csv")

def number(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None

def theta_reference(row, iv):
    """Independent European price difference, in price units per elapsed day.

    No default rate/dividend or contract-size conversion. Inconclusive when
    input evidence or numerical resolution is insufficient.
    """
    kind = row.get("option_type")
    spot, strike, days, rate, dividend = (
        number(row.get(key)) for key in (
            "underlying_model_price", "strike_price", "time_to_expiry_days",
            "risk_free_rate", "dividend_yield"
        )
    )
    if (kind not in ("CALL", "PUT") or None in (spot, strike, days, rate, dividend, iv)
            or min(spot, strike, days, iv) <= 0 or not 0.01 <= iv <= 500):
        return None, None
    sigma = iv / 100
    sign = 1 if kind == "CALL" else -1

    def price(remaining_days):
        t = remaining_days / 365
        width = sigma * math.sqrt(t)
        ds = spot * math.exp(-dividend * t)
        dk = strike * math.exp(-rate * t)
        d1 = (math.log(spot) - math.log(strike) + (rate - dividend) * t) / width + width / 2
        d2 = d1 - width
        n1 = math.erfc(-sign * d1 / math.sqrt(2)) / 2
        n2 = math.erfc(-sign * d2 / math.sqrt(2)) / 2
        value = sign * (ds * n1 - dk * n2)
        if not all(map(math.isfinite, (ds, dk, d1, d2, value))) or value < 0:
            raise ValueError("unresolved reference price")
        return value, max(ds, dk)

    def derivative(h):
        if not 0 < days - h < days < days + h:
            raise ValueError("unresolved time step")
        before, scale_before = price(days + h)
        after, scale_after = price(days - h)
        result = (after - before) / (2 * h)
        # Conservative roundoff screen for cancellation in the price difference.
        noise = 32 * math.ulp(max(scale_before, scale_after)) / h
        return result, noise

    try:
        step = min(0.01, days / 4)
        coarse, noise_coarse = derivative(step)
        fine, noise_fine = derivative(step / 2)
        if not all(map(math.isfinite, (coarse, fine, noise_coarse, noise_fine))):
            return None, None
        # Reporting tolerance, not a tick-size or execution eligibility rule.
        tolerance = 5e-8 + 1e-5 * abs(fine)
        uncertainty = abs(fine - coarse) + max(noise_coarse, noise_fine)
        if uncertainty > tolerance / 2:
            return None, None
        return fine, tolerance
    except (ArithmeticError, ValueError):
        return None, None


def review_record(r):
    issues = []
    warnings = []
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

    theta_ref, theta_tolerance = None, None
    theta_status = "missing"
    if theta is None:
        issues.append("تتا محاسبه نشده")
    else:
        theta_ref, theta_tolerance = theta_reference(r, iv)
        if theta_ref is None:
            theta_status = "unverified"
            issues.append("اعتبارسنجی عددی تتا نامشخص")
        elif abs(theta - theta_ref) > theta_tolerance:
            theta_status = "mismatch"
            issues.append("عدم تطابق تتا با مرجع تفاضل محدود")
        else:
            theta_status = "consistent"
            if theta > 0:
                warnings.append("تتای مثبت (سازگار با مرجع عددی)")

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

    return {
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
        "issues": "؛ ".join(issues),
        "theta_check_status": theta_status,
        "theta_reference_per_day": theta_ref,
        "theta_check_tolerance": theta_tolerance,
        "warnings": "؛ ".join(warnings)
    }

def main():
    with INPUT.open(encoding="utf-8") as f:
        records = json.load(f)
    checked = [review_record(r) for r in records]
    issues_count = Counter(
        issue for r in checked if r["issues"] for issue in set(r["issues"].split("؛ "))
    )
    warnings_count = Counter(
        warning for r in checked if r["warnings"] for warning in set(r["warnings"].split("؛ "))
    )
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

    print(f"هشدارهای غیرمسدودکننده: {sum(bool(r['warnings']) for r in checked)}")
    for warning, count in warnings_count.most_common():
        print(f"{warning}: {count}")

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


if __name__ == "__main__":
    main()
