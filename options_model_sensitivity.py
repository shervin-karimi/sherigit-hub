"""Offline European Black–Scholes scenarios; no market or trading inputs."""

import csv
import math
import sys

from calculate_greeks import (
    DAYS_IN_YEAR,
    DIVIDEND_YIELD,
    RISK_FREE_RATE,
    bs_price,
    calculate_greeks,
    implied_volatility,
    iv_failure_reason,
)

GREEKS = ("delta", "gamma", "theta_per_day", "vega_per_1pct", "rho_per_1pct")
COLUMNS = ("case", "type", "spot", "strike", "days", "rate", "dividend",
           "premium", "input_sigma", "iv", "iv_failure_reason", *GREEKS)


def scenario(label, kind, *, spot=100, strike=100, days=90,
             rate=RISK_FREE_RATE, dividend=DIVIDEND_YIELD, premium=12, sigma=None):
    """With a fixed premium, fit IV; with a fixed sigma, reprice then recover IV."""
    inputs = (spot, strike, days / DAYS_IN_YEAR, rate, dividend)
    if sigma is not None:
        premium = bs_price(kind, *inputs, sigma)
    iv = implied_volatility(kind, premium, *inputs)
    greek_sigma = iv if sigma is None else sigma
    greeks = calculate_greeks(kind, *inputs, greek_sigma) if greek_sigma is not None else None
    reason = iv_failure_reason(kind, premium, *inputs) if iv is None else None
    return {
        "case": label, "type": kind, "spot": spot, "strike": strike, "days": days,
        "rate": rate, "dividend": dividend, "premium": premium, "input_sigma": sigma,
        "iv": iv, "iv_failure_reason": reason,
        **{key: greeks[key] if greeks else None for key in GREEKS},
    }


def scenarios():
    for kind in ("CALL", "PUT"):
        yield scenario("baseline", kind)
        yield scenario("rate_low", kind, rate=0.30)
        yield scenario("rate_if_effective", kind, rate=math.log1p(RISK_FREE_RATE))
        yield scenario("rate_high", kind, rate=0.50)
        yield scenario("days_30", kind, days=30)
        yield scenario("days_7", kind, days=7)
        yield scenario("days_120", kind, days=120)
        yield scenario("dividend_10pct", kind, dividend=0.10)
        for sigma in (0.20, 0.40, 0.80):
            yield scenario("fixed_sigma", kind, sigma=sigma)


def main():
    writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(scenarios())


if __name__ == "__main__":
    main()
