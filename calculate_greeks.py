
import json
import math
import csv
from datetime import datetime
from pathlib import Path

INPUT = Path("data/candidates.json")
OUTPUT_JSON = Path("data/greeks_results.json")
OUTPUT_CSV = Path("data/greeks_results.csv")

# فرض‌های آزمایشی
RISK_FREE_RATE = 0.4394
DIVIDEND_YIELD = 0.0
DAYS_IN_YEAR = 365

MIN_VOL = 0.0001
MAX_VOL = 5.0
BISECTION_STEPS = 150


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def normal_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(option_type, S, K, T, r, q, sigma):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None

    sqrt_t = math.sqrt(T)
    d1 = (
        math.log(S / K)
        + (r - q + 0.5 * sigma ** 2) * T
    ) / (sigma * sqrt_t)

    d2 = d1 - sigma * sqrt_t

    if option_type == "CALL":
        return (
            S * math.exp(-q * T) * normal_cdf(d1)
            - K * math.exp(-r * T) * normal_cdf(d2)
        )

    return (
        K * math.exp(-r * T) * normal_cdf(-d2)
        - S * math.exp(-q * T) * normal_cdf(-d1)
    )


def implied_volatility(option_type, market_price, S, K, T, r, q):
    if market_price is None or market_price <= 0:
        return None

    if S <= 0 or K <= 0 or T <= 0:
        return None

    # کران‌های نظری اختیار اروپایی
    discounted_spot = S * math.exp(-q * T)
    discounted_strike = K * math.exp(-r * T)

    if option_type == "CALL":
        lower_bound = max(
            discounted_spot - discounted_strike, 0
        )
        upper_bound = discounted_spot
    else:
        lower_bound = max(
            discounted_strike - discounted_spot, 0
        )
        upper_bound = discounted_strike

    tolerance = max(1e-8, market_price * 1e-10)

    if market_price < lower_bound - tolerance:
        return None

    if market_price >= upper_bound:
        return None

    low = MIN_VOL
    high = MAX_VOL

    high_price = bs_price(
        option_type, S, K, T, r, q, high
    )

    if high_price is None or high_price < market_price:
        return None

    for _ in range(BISECTION_STEPS):
        mid = (low + high) / 2

        theoretical = bs_price(
            option_type, S, K, T, r, q, mid
        )

        if theoretical is None:
            return None

        if abs(theoretical - market_price) < tolerance:
            return mid

        if theoretical < market_price:
            low = mid
        else:
            high = mid

    return (low + high) / 2


def iv_failure_reason(option_type, price, S, K, T, r, q):
    if (
        option_type not in ("CALL", "PUT")
        or price is None
        or not all(
            isinstance(v, (int, float)) and math.isfinite(v)
            for v in (price, S, K, T, r, q)
        )
        or price <= 0 or S <= 0 or K <= 0 or T <= 0
    ):
        return "invalid_input"

    ds = S * math.exp(-q * T)
    dk = K * math.exp(-r * T)

    lower = max(ds - dk, 0) if option_type == "CALL" else max(dk - ds, 0)
    upper = ds if option_type == "CALL" else dk
    threshold = max(1e-8, price * 1e-10)

    if price < lower - threshold:
        return "below_theoretical_bound"
    if price - lower <= threshold:
        return "near_lower_bound_unidentifiable"
    if price >= upper:
        return "outside_solver_range"

    minimum = bs_price(option_type, S, K, T, r, q, MIN_VOL)
    maximum = bs_price(option_type, S, K, T, r, q, MAX_VOL)

    if minimum is None or maximum is None:
        return "outside_solver_range"
    if price < minimum or price > maximum:
        return "outside_solver_range"

    return None


def calculate_greeks(option_type, S, K, T, r, q, sigma):
    option_type = str(option_type).strip().upper()
    if option_type not in ("CALL", "PUT"):
        raise ValueError("option_type must be CALL or PUT")

    if sigma is None or sigma <= 0 or T <= 0:
        return None

    sqrt_t = math.sqrt(T)

    d1 = (
        math.log(S / K)
        + (r - q + 0.5 * sigma ** 2) * T
    ) / (sigma * sqrt_t)

    d2 = d1 - sigma * sqrt_t

    pdf = normal_pdf(d1)
    discount_q = math.exp(-q * T)
    discount_r = math.exp(-r * T)

    gamma = (
        discount_q * pdf / (S * sigma * sqrt_t)
    )

    vega = S * discount_q * pdf * sqrt_t

    if option_type == "CALL":
        delta = discount_q * normal_cdf(d1)

        theta_year = (
            -S * discount_q * pdf * sigma / (2 * sqrt_t)
            - r * K * discount_r * normal_cdf(d2)
            + q * S * discount_q * normal_cdf(d1)
        )

        rho = K * T * discount_r * normal_cdf(d2)

    else:
        delta = discount_q * (normal_cdf(d1) - 1)

        theta_year = (
            -S * discount_q * pdf * sigma / (2 * sqrt_t)
            + r * K * discount_r * normal_cdf(-d2)
            - q * S * discount_q * normal_cdf(-d1)
        )

        rho = -K * T * discount_r * normal_cdf(-d2)

    return {
        "delta": delta,
        "gamma": gamma,
        "theta_per_day": theta_year / DAYS_IN_YEAR,
        "vega_per_1pct": vega / 100,
        "rho_per_1pct": rho / 100,
        "d1": d1,
        "d2": d2
    }


def get_time_to_expiry(contract):
    maturity = contract.get("maturity_date")

    # اولویت با تاریخ snapshot خود داده‌های اختیار است.
    snapshot_time = contract.get("snapshot_received_at")
    if maturity and snapshot_time:
        try:
            snapshot_date = datetime.fromisoformat(
                snapshot_time
            ).date()
            expiry_date = datetime.strptime(
                str(maturity), "%Y%m%d"
            ).date()

            days = (expiry_date - snapshot_date).days
            if days > 0:
                return days / DAYS_IN_YEAR, days
        except (TypeError, ValueError):
            pass

    # روش قبلی، فقط در صورت نامعتبر بودن تاریخ snapshot.
    reference_date = contract.get("underlying_price_date")
    if maturity and reference_date:
        try:
            expiry_date = datetime.strptime(
                str(maturity), "%Y%m%d"
            ).date()
            quote_date = datetime.strptime(
                str(reference_date), "%Y%m%d"
            ).date()

            days = (expiry_date - quote_date).days
            if days > 0:
                return days / DAYS_IN_YEAR, days
        except (TypeError, ValueError):
            pass

    days = contract.get("remaining_days") or 0
    if days > 0:
        return days / DAYS_IN_YEAR, days

    return None, None



def main():
    with INPUT.open("r", encoding="utf-8") as f:
        contracts = json.load(f)

    results = []

    stats = {
        "total": len(contracts),
        "valid": 0,
        "iv_mid": 0,
        "iv_ask": 0,
        "iv_bid": 0,
	"unverified": 0,
        "failed": 0
    }

    for c in contracts:
        item = dict(c)

        # فرض‌های آزمایشی؛ تا زمان تأیید منبع معتبر، Greeks قابل اتکای معاملاتی نیست.
        item["risk_free_rate"] = RISK_FREE_RATE
        item["dividend_yield"] = DIVIDEND_YIELD
        item["greeks_assumption_status"] = "experimental_unverified"
        stats["unverified"] += 1

        option_type = c.get("option_type")
        closing_spot = c.get("underlying_closing_price") or 0
        S = c.get("underlying_last_price") or 0
        item["underlying_model_price"] = S
        item["underlying_closing_price_reference"] = closing_spot
        K = c.get("strike_price") or 0

        bid = c.get("best_bid") or 0
        ask = c.get("best_ask") or 0

        T, days = get_time_to_expiry(c)

        item["time_to_expiry_days"] = days
        item["iv_mid"] = None
        item["iv_ask"] = None
        item["iv_bid"] = None
        item["delta"] = None
        item["gamma"] = None
        item["theta_per_day"] = None
        item["vega_per_1pct"] = None
        item["rho_per_1pct"] = None
        item["iv_status"] = "invalid_input"
        item["iv_failure_reason"] = None

        if (
            option_type not in ("CALL", "PUT")
            or S <= 0
            or K <= 0
            or T is None
            or bid <= 0
            or ask < bid
            or ask <= 0
        ):
            results.append(item)
            stats["failed"] += 1
            continue

        stats["valid"] += 1

        mid = (bid + ask) / 2

        iv_mid = implied_volatility(
            option_type, mid, S, K, T,
            RISK_FREE_RATE, DIVIDEND_YIELD
        )

        iv_ask = implied_volatility(
            option_type, ask, S, K, T,
            RISK_FREE_RATE, DIVIDEND_YIELD
        )

        iv_bid = implied_volatility(
            option_type, bid, S, K, T,
            RISK_FREE_RATE, DIVIDEND_YIELD
        )

        item["iv_mid"] = (
            round(iv_mid * 100, 8) if iv_mid is not None else None
        )

        item["iv_ask"] = (
            round(iv_ask * 100, 8) if iv_ask is not None else None
        )

        item["iv_bid"] = (
            round(iv_bid * 100, 8) if iv_bid is not None else None
        )

        if iv_mid is None:
            item["iv_failure_reason"] = iv_failure_reason(
                option_type, mid, S, K, T,
                RISK_FREE_RATE, DIVIDEND_YIELD
            )
        else:
            stats["iv_mid"] += 1

            greeks = calculate_greeks(
                option_type, S, K, T,
                RISK_FREE_RATE, DIVIDEND_YIELD, iv_mid
            )

            if greeks:
                for key, value in greeks.items():
                    if key in ("d1", "d2"):
                        item[key] = round(value, 6)
                    else:
                        item[key] = round(value, 8)

                item["iv_status"] = "ok"

        if iv_ask is not None:
            stats["iv_ask"] += 1

        if iv_bid is not None:
            stats["iv_bid"] += 1

        if iv_mid is None and iv_ask is not None:
            item["iv_status"] = "mid_unavailable_ask_available"
        elif iv_mid is None:
            item["iv_status"] = "mid_unavailable"

        results.append(item)


    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    if results:
        with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=list(dict.fromkeys(
                    key for row in results for key in row.keys()
                ))
            )
            writer.writeheader()
            writer.writerows(results)


    print("=" * 48)
    print("BLACK-SCHOLES / GREEKS REPORT")
    print("=" * 48)
    print("Risk-free rate:", RISK_FREE_RATE * 100, "%")
    print("Dividend yield:", DIVIDEND_YIELD * 100, "%")
    print("Total candidates:", stats["total"])
    print("Valid inputs:", stats["valid"])
    print("Contracts with mid IV:", stats["iv_mid"])
    print("Contracts with ask IV:", stats["iv_ask"])
    print("Contracts with bid IV:", stats["iv_bid"])
    print("Contracts with unverified assumptions:", stats["unverified"])
    print("Invalid inputs:", stats["failed"])

    print("\nSample results:")

    shown = 0

    for x in results:
        if x.get("iv_mid") is None:
            continue

        print(
            x["symbol"],
            "|", x["option_type"],
            "| Base:", x["underlying_symbol"],
            "| IV:", x["iv_mid"], "%",
            "| Delta:", x.get("delta"),
            "| Gamma:", x.get("gamma"),
            "| Theta/day:", x.get("theta_per_day"),
            "| Vega/1%:", x.get("vega_per_1pct"),
            "| Days:", x.get("time_to_expiry_days")
        )

        shown += 1

        if shown >= 20:
            break

    print("\nSaved:")
    print(OUTPUT_JSON)
    print(OUTPUT_CSV)




if __name__ == "__main__":
    main()
