"""Read-only audit of a supplied secondary notice and archived TSETMC quotes."""

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import re
import sys

import calculate_greeks as model
import shadow_watch as archive_io


BASE = "خودرو"
BASE_CODE = "65883838195688438"  # Archive identity, not supplied by the notice.
EXPIRY = "20270127"  # 1405/11/07 in the Persian calendar.
JALALI = "1405/11/07"
SIZE = 1000
LIMIT = 10
NAME = re.compile(r"(اختیار[خف]) خودرو-([0-9]+)-([0-9]{4}/[0-9]{2}/[0-9]{2})")


def normalize(value):
    return value.replace("ي", "ی").replace("ك", "ک").strip() if isinstance(value, str) else ""


def notice_side(row, side):
    """Return (classification, discrepancies) for this notice only."""
    name = normalize(row.get(f"lVal30_{side}"))
    parsed = NAME.fullmatch(name)
    base = normalize(row.get("lval30_UA"))
    base_code = row.get("uaInsCode")
    if base != BASE and base_code != BASE_CODE and not parsed:
        return "unrelated", []
    end = row.get("endDate")
    if end and end != EXPIRY and parsed and parsed[3] != JALALI:
        return "different_series", []
    if end and end != EXPIRY and not parsed:
        return "different_series", []

    issues = []

    def compare(field, actual, expected):
        if actual is None or actual == "":
            issues.append(f"missing:{field}")
        elif actual != expected:
            issues.append(f"mismatch:{field}")

    compare("underlying_symbol", base, BASE)
    compare("underlying_id", base_code, BASE_CODE)
    compare("endDate", end, EXPIRY)
    compare("contractSize", row.get("contractSize"), SIZE)
    symbol = normalize(row.get(f"lVal18AFC_{side}"))
    prefix = "ضخود" if side == "C" else "طخود"
    if not symbol:
        issues.append("missing:option_symbol")
    elif not re.fullmatch(prefix + r"[0-9]+", symbol):
        issues.append("mismatch:option_symbol")
    if not archive_io.positive_id(row.get(f"insCode_{side}")):
        issues.append("missing_or_invalid:option_id")
    strike = row.get("strikePrice")
    if type(strike) is not int or strike <= 0:
        issues.append("missing_or_invalid:strikePrice")
    if not parsed:
        issues.append("unparseable:long_name" if name else "missing:long_name")
    else:
        compare("long_name_type", parsed[1], "اختیارخ" if side == "C" else "اختیارف")
        compare("long_name_expiry", parsed[3], JALALI)
        if type(strike) is int and strike > 0:
            compare("long_name_strike", int(parsed[2]), strike)
    return ("incomplete_or_conflicting" if issues else "archive_family_match"), issues


def shortlist(states):
    """Diagnostic order only; make no change to either policy gate."""
    rejected = [(code, row) for code, row in states.items()
                if row["candidate_eligible"] and not row["rank_eligible"]]
    return sorted(rejected, key=lambda item: (
        item[1]["spread_percent"], -item[1]["trade_count"], -item[1]["trade_volume"], item[0]
    ))[:LIMIT]


def model_values(row, received_at):
    years, _ = model.get_time_to_expiry(dict(row, snapshot_received_at=received_at))
    mid = model.quote_midpoint(row["best_bid"], row["best_ask"])
    args = (row["option_type"], mid, row["underlying_last_price"], row["strike_price"],
            years, model.RISK_FREE_RATE, model.DIVIDEND_YIELD)
    iv = model.implied_volatility(*args)
    if iv is None:
        return "—", "—", model.iv_failure_reason(*args)
    greeks = model.calculate_greeks(args[0], *args[2:], iv)
    return f"{100 * iv:.4f}", f"{greeks['delta']:.5f}" if greeks else "—", "—"


def report(archive):
    lines = [
        "# Iran Khodro contract matching and observational diagnostics",
        "",
        "**Not a trading signal.** `execution_eligible=false`; no fills, P&L or account activity.",
        "",
        "- Supplied secondary lead (not accessed): [Shakhesban republication]"
        "(https://www.shakhesban.com/stocks/messages/280381), notice `181/140511566`, "
        "dated `1405/08/04`; خودرو, expiry `1405/11/07` (`2027-01-27`).",
        "- Reported, **not verified**: 1,000 shares, European/non-automatic exercise, "
        "cash settlement by mutual agreement, physical settlement at expiry. "
        "These terms are not added to any instrument metadata.",
        "- The supplied summary has no strike schedule, venue instrument IDs or amendments. "
        "A family match means the archive ID/name/type and strike fields agree internally, "
        "and the archive expiry and numeric multiplier agree with the lead; "
        "it does not certify exact membership in the notice's roster or the multiplier's unit.",
        "- Calendar cross-check: local ICU Persian calendar (via Node Intl). "
        "Dates are date-only, not trading/exercise cutoffs.",
        f"- Archive source: `{archive_io.SOURCE}`; the خودرو base identifier in the archive is "
        f"`{BASE_CODE}`. The family table requires `endDate={EXPIRY}` and `contractSize={SIZE}`.",
        "- Reproduce: `python -B contract_diagnostics.py` from the repo root (local archive required).",
        "",
        "## Archive coverage and gate counts",
        "",
        "| Snapshot (Tehran receipt, not quote time) | Body SHA-256 | Family sides | "
        "Metadata issues | Other خودرو expiries (sides) | All contracts / candidates / rank eligible |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    matches, issues = defaultdict(list), []
    identities = {}
    for _, attempt, path, meta in archive_io.records_in_order(archive):
        states = archive_io.contract_states(path, meta)
        body, _ = archive_io.read_object(path, archive_io.MAX_BODY, meta["body_sha256"])
        latest_matches, others = set(), Counter()
        issues_before = len(issues)
        for row in body["instrumentOptMarketWatch"]:
            for side in ("C", "P"):
                code = row.get(f"insCode_{side}")
                status, discrepancies = notice_side(row, side)
                if status == "different_series" and code in states:
                    others[row["endDate"]] += 1
                if status == "incomplete_or_conflicting":
                    issues.append((attempt, code, row.get(f"lVal18AFC_{side}"), ", ".join(discrepancies)))
                if status != "archive_family_match":
                    continue
                key = (row["strikePrice"], side, row[f"lVal18AFC_{side}"], code)
                archive_io.require(identities.setdefault(code, key) == key,
                                   f"family identity changed: {code}")
                matches[key].append(attempt)
                latest_matches.add(code)
        obs = meta["receipt_tehran"]
        n_candidate = sum(item["candidate_eligible"] for item in states.values())
        n_rank = sum(item["rank_eligible"] for item in states.values())
        lines.append(
            f"| {attempt}: `{obs}` | `{meta['body_sha256']}` | {len(latest_matches)} | "
            f"{len(issues) - issues_before} | "
            f"{', '.join(f'{date}: {count}' for date, count in sorted(others.items())) or '—'} | "
            f"{len(states)} / {n_candidate} / {n_rank} |"
        )

    lines += ["", "## Instrument matches in the archive", "",
              "| Strike (raw) | Side | Symbol | TSETMC ID | Snapshots |",
              "| ---: | --- | --- | --- | --- |"]
    for (strike, side, symbol, code), seen in sorted(matches.items()):
        kind = "CALL" if side == "C" else "PUT"
        lines.append(f"| {strike} | {kind} | {symbol} | `{code}` | {', '.join(map(str, seen))} |")
    lines += ["", "### Conflicting or incomplete metadata", ""]
    lines += ([f"- Snapshot {n}, `{code}`, {symbol}: `{reason}`."
               for n, code, symbol, reason in issues] or
              ["- None among the apparent target series in the archive. Other expiries are "
               "different series, not mismatches within this expiry. In particular, "
               "`ضخود1318 / طخود1318` expires `2026-12-30`, not `2027-01-27`."])
    family_gate = Counter(
        states[code]["candidate_rejection"] or states[code]["rank_rejection"] or "rank_eligible"
        for code in latest_matches
    )
    lines += ["", f"Latest-snapshot family first gate results: "
              f"{'; '.join(f'{reason}: {count}' for reason, count in sorted(family_gate.items())) or 'none'}."]
    for code in sorted(latest_matches):
        row = states[code]
        if row["candidate_eligible"]:
            lines.append(f"- Family scanner candidate {row['symbol']} (`{code}`): "
                         f"{row['rank_rejection'] or 'passed the ranking function'}.")
    lines += ["", "## Latest snapshot: market-wide observational shortlist", "",
              "Only existing scanner candidates rejected by the unchanged eligibility function. "
              "Ordered by observed spread ascending, trade count and volume descending, then ID; "
              "this order has no eligibility meaning. Rejections are the actual first failing "
              "ranking rule, not a list of rules that might fail later.",
              "", f"IV/Delta use raw bid/ask midpoint and reported underlying last, with "
              f"experimental `r={model.RISK_FREE_RATE}`, `q={model.DIVIDEND_YIELD}` and date-based ACT/365. "
              "No verifiable quote/spot times, sequence, executable depth, or official exercise/settlement terms.",
              "", "| # | Symbol / type / ID | Notice family? | Days | Bid/ask | Depth bid/ask | "
              "Trades / volume | Spread % | Mid IV % / Delta | IV / Greeks status | First rank rejection |",
              "| ---: | --- | --- | ---: | --- | --- | --- | ---: | --- | --- | --- |"]
    for index, (code, row) in enumerate(shortlist(states), 1):
        iv, delta, failure = model_values(row, obs)
        lines.append(
            f"| {index} | {row['symbol']} / {row['option_type']} / `{code}` | "
            f"{'archive match' if code in latest_matches else 'no'} | {row['remaining_days']} | "
            f"{row['best_bid']}/{row['best_ask']} | {row['bid_volume']}/{row['ask_volume']} | "
            f"{row['trade_count']}/{row['trade_volume']} | {row['spread_percent']:.6f} | {iv} / {delta} | "
            f"`{row['iv_status']}` / `{row['greeks_assumption_status']}`"
            f"{f' (`{failure}`)' if failure != '—' else ''} | {row['rank_rejection']} |"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=archive_io.DEFAULT_ARCHIVE)
    args = parser.parse_args()
    try:
        output = report(args.archive)
    except (OSError, ValueError) as error:
        print(f"Contract diagnostics stopped: {error}", file=sys.stderr)
        return 1
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
