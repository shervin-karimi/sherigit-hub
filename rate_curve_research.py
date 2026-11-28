"""Offline research-only IRR zero-coupon discount curve; never a trading rate."""
import argparse
from datetime import date
import hashlib
import json
import math
from pathlib import Path

DAYS_IN_YEAR = 365
MAX_INPUT_BYTES = 1024 * 1024
MAX_QUOTE_AGE_DAYS = 4  # research policy, not an exchange rule


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def iso_day(value):
    if not isinstance(value, str):
        raise ValueError("expected ISO date")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("expected YYYY-MM-DD")
    return result


def positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("expected finite positive numeric value")
    return float(value)


def required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing provenance or identity")
    return value


def curve(snapshot, as_of):
    """Use one prior finalized close date; no mixed quote dates, no lookahead."""
    as_of = iso_day(as_of)
    quote_day = iso_day(snapshot["quote_date"])
    available_day = iso_day(snapshot["available_date"])
    if not quote_day <= available_day < as_of:
        raise ValueError("only data available before the valuation date are allowed")
    if (as_of - quote_day).days > MAX_QUOTE_AGE_DAYS:
        raise ValueError("stale quote date")
    if snapshot["currency"] != "IRR" or snapshot["price_basis"] != "finalized_close_per_bond":
        raise ValueError("IRR finalized close per bond required")
    if snapshot["classification"] not in ("synthetic", "unverified_observation"):
        raise ValueError("research-only classification required")
    required_text(snapshot["price_source"])
    rows = snapshot["bonds"]
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("at least two bonds required")
    nodes = []
    identifiers = set()
    maturities = set()
    for row in rows:
        identifier = required_text(row["id"])
        maturity = iso_day(row["maturity_date"])
        if identifier in identifiers or maturity in maturities:
            raise ValueError("duplicate ID or maturity; resolve upstream")
        identifiers.add(identifier)
        maturities.add(maturity)
        if row["issuer_type"] != "iran_government" or row["cashflow_type"] != "single_redemption":
            raise ValueError("only government single-redemption bonds supported")
        terms_source = required_text(row["terms_source"])
        if iso_day(row["quote_date"]) != quote_day:
            raise ValueError("mixed quote dates")
        if maturity <= as_of:
            raise ValueError("expired or same-day maturity")
        price = positive(row["close_irr"])
        redemption = positive(row["redemption_irr"])
        volume = positive(row["trade_volume"])
        # Reprice a stale close is not justified. Estimate a flat continuous
        # zero rate at each node on the original quote date, then roll its
        # maturity to the valuation date. This is a disclosed research proxy.
        quote_years = (maturity - quote_day).days / DAYS_IN_YEAR
        r = (math.log(redemption) - math.log(price)) / quote_years
        days = (maturity - as_of).days
        log_d = -r * days / DAYS_IN_YEAR
        nodes.append({"id": identifier, "maturity_date": maturity.isoformat(),
                      "days": days, "rate_continuous": r, "log_discount": log_d,
                      "close_irr": price, "redemption_irr": redemption,
                      "trade_volume": volume, "terms_source": terms_source})
    return sorted(nodes, key=lambda node: node["days"])


def estimate(nodes, days):
    if type(days) is not int or days <= 0:
        raise ValueError("positive integer days required")
    for n in nodes:
        if n["days"] == days:
            return result(days, n["log_discount"], [n["id"]])
    for a, b in zip(nodes, nodes[1:]):
        if a["days"] < days < b["days"]:
            weight = (days - a["days"]) / (b["days"] - a["days"])
            log_d = (1 - weight) * a["log_discount"] + weight * b["log_discount"]
            return result(days, log_d, [a["id"], b["id"]])
    return {"days": days, "status": "outside_curve_coverage"}


def result(days, log_d, ids):
    rate = -log_d * DAYS_IN_YEAR / days
    try:
        discount, effective = math.exp(log_d), math.expm1(rate)
    except OverflowError as error:
        raise ValueError("rate outside numerical range") from error
    if not math.isfinite(effective) or not math.isfinite(discount) or discount <= 0:
        raise ValueError("rate outside numerical range")
    return {"days": days, "status": "research_estimate", "rate_continuous": rate,
            "rate_effective_annual": effective, "discount_factor": discount,
            "node_ids": ids}


def report(raw, as_of, tenors):
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    snapshot = json.loads(raw, object_pairs_hook=unique_object)
    nodes = curve(snapshot, as_of)
    return {"greeks_assumption_status": "experimental_unverified", "execution_eligible": False,
            "classification": snapshot["classification"], "as_of": as_of,
            "quote_date": snapshot["quote_date"], "available_date": snapshot["available_date"],
            "price_source": snapshot["price_source"], "input_sha256": hashlib.sha256(raw).hexdigest(),
            "method": "ACT/365; prior close zero rates rolled flat; log-discount interpolation; no extrapolation",
            "nodes": nodes, "estimates": [estimate(nodes, days) for days in tenors]}


def demo_snapshot():
    rows = []
    for i, (maturity, price) in enumerate((("2027-01-27", 940000), ("2027-05-27", 840000))):
        rows.append(dict(id=f"synthetic-{i}", maturity_date=maturity,
                         quote_date="2026-11-25", close_irr=price, redemption_irr=1000000,
                         issuer_type="iran_government", cashflow_type="single_redemption",
                         terms_source="synthetic fixture", trade_volume=10))
    return dict(classification="synthetic", quote_date="2026-11-25", available_date="2026-11-25",
                currency="IRR", price_basis="finalized_close_per_bond", price_source="synthetic fixture", bonds=rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--demo", action="store_true")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--days", nargs="+", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        if args.demo:
            raw = json.dumps(demo_snapshot()).encode()
        else:
            with args.input.open("rb") as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
        output = report(raw, args.as_of, args.days)
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        parser.exit(1, f"Offline rate research stopped: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
