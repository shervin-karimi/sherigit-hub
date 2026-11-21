import csv
import math
from pathlib import Path
from sys import float_info

from calculate_greeks import (
    DAYS_IN_YEAR, DIVIDEND_YIELD as Q, IV_PERCENT_DECIMALS, MAX_VOL, MIN_VOL,
    RISK_FREE_RATE as R, bs_price, iv_price_tolerance, quote_midpoint,
)

INPUT = Path("data/greeks_results.csv")


def positive_number(raw):
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("expected a finite positive number")
    return value


def market_midpoint(row):
    explicit = (row.get("option_mid_price") or "").strip()
    bid = (row.get("best_bid") or "").strip()
    ask = (row.get("best_ask") or "").strip()
    stored = positive_number(explicit) if explicit else None
    if not bid and not ask and stored is not None:
        return stored
    mid = quote_midpoint(positive_number(bid), positive_number(ask))
    if mid is None or (stored is not None and stored not in (mid, round(mid, 2))):
        raise ValueError("invalid or inconsistent midpoint")
    return mid


def reconstruct(row):
    S = positive_number(row["underlying_model_price"])
    K = positive_number(row["strike_price"])
    T = positive_number(row["time_to_expiry_days"]) / DAYS_IN_YEAR
    iv = positive_number(row["iv_mid"]) / 100
    market = market_midpoint(row)
    kind = row["option_type"]
    if not MIN_VOL <= iv <= MAX_VOL or market < float_info.min:
        raise ValueError("outside numerical range")

    # Compare the quote with the price interval implied by rounding IV to
    # eight decimal places in percent. No tick-size or absolute error floor.
    half_step = 0.5 * 10**(-IV_PERCENT_DECIMALS) / 100 + math.ulp(iv)
    low = bs_price(kind, S, K, T, R, Q, max(MIN_VOL, iv - half_step))
    theoretical = bs_price(kind, S, K, T, R, Q, iv)
    high = bs_price(kind, S, K, T, R, Q, min(MAX_VOL, iv + half_step))
    if any(value is None or not math.isfinite(value) for value in (low, theoretical, high)):
        raise ValueError("invalid model output")
    tolerance = iv_price_tolerance(market)
    within_tolerance = (
        high > 0 and low - market <= tolerance and market - high <= tolerance
    )
    return market, theoretical, within_tolerance


def main():
    if not INPUT.exists():
        print("ERROR: CSV file not found")
        return

    checked = 0
    skipped = 0
    errors = []
    incompatible = []

    with INPUT.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                market, theoretical, within_tolerance = reconstruct(row)
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue

            error = theoretical - market
            checked += 1
            errors.append(abs(error))
            if not within_tolerance:
                incompatible.append((row.get("symbol") or "<unknown>", market, theoretical, error))

    print("=== Greeks numerical validation ===")
    print("Contracts checked:", checked)
    print("Contracts skipped:", skipped)
    print("Assumed risk-free rate:", R * 100, "%")
    print("Dividend yield assumption:", Q * 100, "%")
    print("Rate source independently verified: NO")
    print("Independent model validation: NO")
    print("This test only checks numerical reconstruction.")

    if errors:
        print("Mean absolute reconstruction error:",
              format(math.fsum(error / len(errors) for error in errors), ".8g"))
        print("Maximum absolute reconstruction error:",
              format(max(errors), ".8g"))
        print("Contracts above tolerance:", len(incompatible))

        for symbol, market, theoretical, error in incompatible[:10]:
            print(
                symbol,
                "| Market:", market,
                "| Model:", format(theoretical, ".8g"),
                "| Error:", format(error, ".8g")
            )

if __name__ == "__main__":
    main()
