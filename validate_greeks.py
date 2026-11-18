import csv
import math
from pathlib import Path

INPUT = Path("data/greeks_results.csv")

# فرض آزمایشی؛ منبع مستقل تأیید نشده است.
R = 0.4394
Q = 0.0
DAYS_IN_YEAR = 365

def cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_price(S, K, T, sigma, option_type):
    if min(S, K, T, sigma) <= 0:
        return None

    root_t = math.sqrt(T)
    d1 = (
        math.log(S / K)
        + (R - Q + 0.5 * sigma**2) * T
    ) / (sigma * root_t)
    d2 = d1 - sigma * root_t

    if option_type == "CALL":
        return (
            S * math.exp(-Q*T) * cdf(d1)
            - K * math.exp(-R*T) * cdf(d2)
        )

    if option_type == "PUT":
        return (
            K * math.exp(-R*T) * cdf(-d2)
            - S * math.exp(-Q*T) * cdf(-d1)
        )

    return None

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
                S = float(row["underlying_model_price"])
                K = float(row["strike_price"])
                days = float(row["time_to_expiry_days"])
                market = float(row["option_mid_price"])
                iv = float(row["iv_mid"]) / 100
                option_type = row["option_type"]

                if min(S, K, days, market, iv) <= 0:
                    raise ValueError("non-positive input")

                T = days / DAYS_IN_YEAR
                theoretical = bs_price(
                    S, K, T, iv, option_type
                )

                if theoretical is None:
                    raise ValueError("invalid model output")

                error = theoretical - market
                checked += 1
                errors.append(abs(error))

                if abs(error) > max(0.01, market * 1e-6):
                    incompatible.append(
                        (row["symbol"], market, theoretical, error)
                    )

            except (ValueError, TypeError, KeyError):
                skipped += 1

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
              round(sum(errors) / len(errors), 8))
        print("Maximum absolute reconstruction error:",
              round(max(errors), 8))
        print("Contracts above tolerance:", len(incompatible))

        for symbol, market, theoretical, error in incompatible[:10]:
            print(
                symbol,
                "| Market:", market,
                "| Model:", round(theoretical, 6),
                "| Error:", round(error, 6)
            )

if __name__ == "__main__":
    main()
