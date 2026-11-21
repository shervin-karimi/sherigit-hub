"""Existing shortlist filters, shared by the scanner and offline shadow watch."""

MIN_TRADES = 30
MAX_SPREAD = 5
MIN_DAYS = 21


def evaluate_candidate(contract):
    """Return (shortlisted row, first rejection code), without I/O."""
    bid = contract.get("best_bid") or 0
    ask = contract.get("best_ask") or 0
    spot = contract.get("underlying_closing_price") or 0
    intrinsic = contract.get("intrinsic_value")

    if bid <= 0 or ask < bid or spot <= 0:
        return None, "invalid_quote_or_closing_spot"
    if (contract.get("trade_count") or 0) < MIN_TRADES:
        return None, "insufficient_trades"
    if (contract.get("trade_volume") or 0) <= 0:
        return None, "no_trade_volume"
    if (contract.get("bid_volume") or 0) <= 0:
        return None, "no_bid_depth"
    if (contract.get("ask_volume") or 0) <= 0:
        return None, "no_ask_depth"
    if (contract.get("remaining_days") or 0) < MIN_DAYS:
        return None, "near_expiry"

    mid = (bid + ask) / 2
    spread = (ask - bid) / mid * 100
    if spread > MAX_SPREAD:
        return None, "wide_spread"

    ask_time_value = ask - intrinsic
    if ask_time_value < 0:
        return None, "ask_below_intrinsic"

    item = dict(contract)
    item["spread_percent"] = round(spread, 3)
    item["ask_time_value"] = round(ask_time_value, 2)
    item["ask_premium_percent_of_spot"] = round(ask / spot * 100, 3)
    return item, None
