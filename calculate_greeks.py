
import json
import math
import csv
from datetime import datetime
from pathlib import Path
from sys import float_info

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
IV_PERCENT_DECIMALS = 8
SQRT_2 = math.sqrt(2)
SQRT_2PI = math.sqrt(2 * math.pi)
LOG_SQRT_2PI = math.log(SQRT_2PI)
LOG_HALF_MIN_SUBNORMAL = math.log(math.ulp(0.0)) - math.log(2)


def normal_cdf(x):
    return 0.5 * math.erfc(-x / SQRT_2)


def normal_pdf(x):
    return math.exp(-0.5 * x * x) / SQRT_2PI


def finite_numbers(*values):
    try:
        return all(type(value) in (int, float) and math.isfinite(value) for value in values)
    except OverflowError:
        return False


def quote_midpoint(bid, ask):
    if not finite_numbers(bid, ask) or not 0 < bid <= ask:
        return None
    total = bid + ask
    return total / 2 if finite_numbers(total) else bid + (ask - bid) / 2


def _pricing_inputs(option_type, S, K, T, r, q):
    if option_type not in ("CALL", "PUT") or not finite_numbers(S, K, T, r, q) or min(S, K, T) <= 0:
        return None
    rt, qt = r * float(T), q * float(T)
    if not finite_numbers(rt, qt):
        return None
    try:
        ds, dk = S * math.exp(-qt), K * math.exp(-rt)
    except OverflowError:
        return None
    if not finite_numbers(ds, dk) or min(ds, dk) < float_info.min:
        return None
    ratio = S / K
    if 0.5 <= ratio <= 2:
        log_ratio = math.log1p((S - K) / K)
    else:
        log_ratio = math.log(ratio) if float_info.min <= ratio < math.inf else math.log(S) - math.log(K)
    carry = (r - q) * float(T)
    moneyness = log_ratio + carry if math.isfinite(carry) else math.fsum((log_ratio, rt, -qt))
    if not math.isfinite(moneyness):
        return None
    # Avoid subtracting two independently rounded discounted prices near parity.
    gap = (max(ds, dk) * -math.expm1(-abs(moneyness)) if abs(moneyness) < math.log(2)
           else abs(ds - dk))
    return ds, dk, moneyness, math.sqrt(T), gap

def _d_values(inputs, sigma):
    width = sigma * inputs[3]
    if not math.isfinite(width) or width <= 0:
        return None
    z = inputs[2] / width
    return z + width / 2, z - width / 2


def _weighted_cdf(scale, x):
    cdf = normal_cdf(x)
    if cdf >= float_info.min or x >= 0:
        return scale * cdf
    if not math.isfinite(x):
        return 0.0
    # Far left normal tail: use the asymptotic Mills ratio before multiplying.
    # At the binary64 underflow threshold these eight terms exceed its precision.
    term, series = 1.0, 1.0
    for n in range(1, 9):
        term *= -(2 * n - 1) / (x * x)
        series += term
    return math.exp(math.log(scale) - 0.5 * x * x - LOG_SQRT_2PI - math.log(-x) + math.log(series))


def _integral(f, budget):
    """Adaptive Simpson on [0, 1]; its error estimate is dimensionless."""
    def integrate(a, b, left, mid, right, whole, tolerance, depth):
        m = (a + b) / 2
        lm, rm = f((a + m) / 2), f((m + b) / 2)
        l = (m - a) * (left + 4 * lm + mid) / 6
        r = (b - m) * (mid + 4 * rm + right) / 6
        error = l + r - whole
        if abs(error) <= 15 * tolerance:
            return l + r + error / 15
        if not depth:
            return None
        first = integrate(a, m, left, lm, mid, l, tolerance / 2, depth - 1)
        second = integrate(m, b, mid, rm, right, r, tolerance / 2, depth - 1)
        return first + second if first is not None and second is not None else None

    left, mid, right = f(0), f(.5), f(1)
    return integrate(0, 1, left, mid, right, (left + 4 * mid + right) / 6, budget, 48)


def _integrated_vega(scale, z, width, endpoint):
    # OTM time value / scale = ∫[0,w] φ(-|m|/u + u/2) du.
    # Factor out the largest density on the interval. In a deep tail the
    # endpoint slope gives the scale of the narrow layer that carries the mass.
    peak = max(endpoint, 0.0)
    if not math.isfinite(peak):
        return 0.0
    log_scale = math.log(scale) + math.log(width) - LOG_SQRT_2PI - 0.5 * peak * peak
    if log_scale < LOG_HALF_MIN_SUBNORMAL:
        return 0.0  # Even the upper bound on the positive integral rounds to zero.

    def density(x):
        if not x:
            return 0.0
        if peak > 0:
            shift = (1 - x) * (z / x + width / 2)
            return math.exp(-shift * (peak + shift / 2))
        d = z / x - width * x / 2
        return math.exp(-0.5 * d * d)

    integral = _integral(density, 1e-13 / max(1, peak * (z + width / 2)))
    return math.exp(log_scale + math.log(integral)) if integral and integral > 0 else None


def _time_value(inputs, sigma):
    ds, dk, m, sqrt_t, _ = inputs
    width = sigma * sqrt_t
    if not math.isfinite(width) or width <= 0:
        return None
    small, large = (dk, ds) if m >= 0 else (ds, dk)
    if m == 0:
        # Exact ATM-forward identity; use the leading term only when omitted
        # O(w²) relative terms are below floating-point rounding.
        if width <= math.sqrt(float_info.epsilon):
            return (small / SQRT_2PI) * width
        return small * math.erf(width / (2 * SQRT_2))
    z = abs(m) / width
    endpoint = z - width / 2
    pos = _weighted_cdf(small, -endpoint)
    neg = _weighted_cdf(large, -z - width / 2)
    difference = pos - neg
    # Switch algorithms, not prices: avoid losing more than ~2 decimal digits
    # to subtraction, or subtracting rounded/subnormal weighted tail terms.
    if pos >= float_info.min and difference > .01 * pos and difference >= float_info.min:
        return difference
    return _integrated_vega(small, z, width, endpoint)


def _bounds(option_type, inputs):
    ds, dk, m, _, gap = inputs
    return ((gap if m > 0 else 0.0), ds) if option_type == "CALL" else ((gap if m < 0 else 0.0), dk)


def price_bounds(option_type, S, K, T, r, q):
    inputs = _pricing_inputs(option_type, S, K, T, r, q)
    return _bounds(option_type, inputs) if inputs is not None else None


def _upper_gap(inputs, sigma):
    points = _d_values(inputs, sigma)
    if points is None:
        return None
    d1, d2 = points
    ds, dk = inputs[:2]
    return _weighted_cdf(ds, -d1) + _weighted_cdf(dk, d2)


def _price(option_type, inputs, sigma):
    time_value = _time_value(inputs, sigma)
    if time_value is None:
        return None
    lower, upper = _bounds(option_type, inputs)
    if time_value > min(inputs[:2]) / 2:
        price = upper - _upper_gap(inputs, sigma)
    else:
        price = lower + time_value
    return price if math.isfinite(price) and lower <= price <= upper else None


def bs_price(option_type, S, K, T, r, q, sigma):
    inputs = _pricing_inputs(option_type, S, K, T, r, q)
    if inputs is None or not finite_numbers(sigma) or sigma <= 0:
        return None
    return _price(option_type, inputs, sigma)


def iv_price_tolerance(price):
    # Relative repricing error only: a fixed absolute floor swallows tail prices.
    return price * 1e-10


def _iv_problem(option_type, price, S, K, T, r, q):
    inputs = _pricing_inputs(option_type, S, K, T, r, q)
    if inputs is None or not finite_numbers(price) or price <= 0:
        return None, "invalid_input"
    if price < float_info.min:
        return None, "subnormal_premium_unidentifiable"
    lower, upper = _bounds(option_type, inputs)
    if price < lower:
        return None, "below_theoretical_bound"
    if price > upper:
        return None, "above_theoretical_bound"
    # The rounding intervals of a premium and bound one float apart touch.
    if lower > 0 and price <= math.nextafter(lower, math.inf):
        return None, "near_lower_bound_unidentifiable"
    if price >= math.nextafter(upper, -math.inf):
        return None, "near_upper_bound_unidentifiable"
    time_value, deficit = price - lower, upper - price
    from_upper = deficit < time_value
    target = deficit if from_upper else time_value
    return (inputs, from_upper, target, min(iv_price_tolerance(price), target * 1e-6)), None


def _iv_value(inputs, from_upper, sigma):
    return _upper_gap(inputs, sigma) if from_upper else _time_value(inputs, sigma)


def _solve_iv(option_type, price, S, K, T, r, q):
    problem, reason = _iv_problem(option_type, price, S, K, T, r, q)
    if problem is None:
        return None, reason
    inputs, from_upper, target, tolerance = problem

    low, high = MIN_VOL, MAX_VOL
    a, b = (_iv_value(inputs, from_upper, x) for x in (low, high))
    if a is None or b is None or (a < b if from_upper else a > b):
        return None, "price_evaluation_failed"
    if a == b:
        return None, "volatility_unidentifiable" if target == a else "outside_solver_range"
    # The gap is evaluated directly, but a published premium is rounded. At an
    # exact repriced bracket endpoint recognize that representation, not an epsilon.
    for sigma, value in ((low, a), (high, b)):
        if target == value or (abs(target - value) <= math.ulp(price) / 2
                               and _price(option_type, inputs, sigma) == price):
            return sigma, None
    if not min(a, b) <= target <= max(a, b):
        return None, "outside_solver_range"

    for _ in range(BISECTION_STEPS):
        mid = (low + high) / 2
        if not low < mid < high:
            break
        theoretical = _iv_value(inputs, from_upper, mid)
        if theoretical is None:
            return None, "price_evaluation_failed"
        if theoretical > 0 and abs(theoretical - target) <= tolerance:
            return mid, None
        if (from_upper and theoretical > target) or (not from_upper and theoretical < target):
            low = mid
        else:
            high = mid
    return None, "price_reconstruction_failed"


def implied_volatility(option_type, market_price, S, K, T, r, q):
    return _solve_iv(option_type, market_price, S, K, T, r, q)[0]


def iv_failure_reason(option_type, price, S, K, T, r, q):
    return _solve_iv(option_type, price, S, K, T, r, q)[1]


def calculate_greeks(option_type, S, K, T, r, q, sigma):
    option_type = str(option_type).strip().upper()
    if option_type not in ("CALL", "PUT"):
        raise ValueError("option_type must be CALL or PUT")

    inputs = _pricing_inputs(option_type, S, K, T, r, q)
    if inputs is None or not finite_numbers(sigma) or sigma <= 0:
        return None

    points = _d_values(inputs, sigma)
    if points is None or not finite_numbers(*points):
        return None
    d1, d2 = points
    ds, dk, _, sqrt_t, _ = inputs
    pdf = normal_pdf(d1)
    discount_q = ds / S

    gamma = (discount_q * pdf / (sigma * sqrt_t)) / S
    vega = ds * pdf * sqrt_t

    if option_type == "CALL":
        delta = discount_q * normal_cdf(d1)

        theta_year = (
            -ds * pdf * sigma / (2 * sqrt_t)
            - r * dk * normal_cdf(d2)
            + q * ds * normal_cdf(d1)
        )

        rho = dk * T * normal_cdf(d2)

    else:
        delta = -discount_q * normal_cdf(-d1)

        theta_year = (
            -ds * pdf * sigma / (2 * sqrt_t)
            + r * dk * normal_cdf(-d2)
            - q * ds * normal_cdf(-d1)
        )

        rho = -dk * T * normal_cdf(-d2)

    greeks = {
        "delta": delta,
        "gamma": gamma,
        "theta_per_day": theta_year / DAYS_IN_YEAR,
        "vega_per_1pct": vega / 100,
        "rho_per_1pct": rho / 100,
        "d1": d1,
        "d2": d2
    }
    return greeks if finite_numbers(*greeks.values()) else None


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
            # A known as-of date is final, including on or after expiry.
            return (days / DAYS_IN_YEAR, days) if days > 0 else (None, None)
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
            return (days / DAYS_IN_YEAR, days) if days > 0 else (None, None)
        except (TypeError, ValueError):
            pass

    days = contract.get("remaining_days") or 0
    if finite_numbers(days) and days > 0:
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
        mid = quote_midpoint(bid, ask)

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
            or not finite_numbers(S, K)
            or S <= 0
            or K <= 0
            or T is None
            or mid is None
        ):
            results.append(item)
            stats["failed"] += 1
            continue

        stats["valid"] += 1

        iv_mid, failure_reason = _solve_iv(
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
            round(iv_mid * 100, IV_PERCENT_DECIMALS) if iv_mid is not None else None
        )

        item["iv_ask"] = (
            round(iv_ask * 100, IV_PERCENT_DECIMALS) if iv_ask is not None else None
        )

        item["iv_bid"] = (
            round(iv_bid * 100, IV_PERCENT_DECIMALS) if iv_bid is not None else None
        )

        if iv_mid is None:
            item["iv_failure_reason"] = failure_reason
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
