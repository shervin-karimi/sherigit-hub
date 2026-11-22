"""Offline, non-executable observation replay for archived TSETMC options data."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from candidate_rules import evaluate_candidate
from rank_candidates import check_contract


ROOT = Path(__file__).resolve().parent
DEFAULT_ARCHIVE = ROOT / "data/observations/tsetmc-options/20261121T065635Z"
SOURCE = "https://cdn.tsetmc.com/api/Instrument/GetInstrumentOptionMarketWatch/1"
DATA_CLASS = "real_observation_unverified"
MAX_BODY = 8 * 1024 * 1024
MAX_META = 64 * 1024
MAX_RECORDS = 100
ANALYSIS_SCRIPTS = ("build_contracts.py", "calculate_greeks.py")
MODEL_FILES = (*ANALYSIS_SCRIPTS, "candidate_rules.py", "rank_candidates.py")
IDENTITY = ("option_type", "underlying_code", "strike_price", "maturity_date", "contract_size")
PRICE_FIELDS = ("best_bid", "best_ask")
LIQUIDITY_FIELDS = ("bid_volume", "ask_volume", "trade_count", "trade_volume")
OBSERVED_NUMBERS = (*PRICE_FIELDS, *LIQUIDITY_FIELDS, "remaining_days",
                    "underlying_closing_price", "underlying_last_price")
ELIGIBILITY_FIELDS = ("candidate_eligible", "rank_eligible", "candidate_rejection", "rank_rejection")
COMPARE_FIELDS = (*PRICE_FIELDS, "spread_percent", *LIQUIDITY_FIELDS, *ELIGIBILITY_FIELDS,
                  "two_sided", "iv_status", "greeks_assumption_status")


class ArchiveError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ArchiveError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "repeated JSON key")
        result[key] = value
    return result


def invalid_constant(_):
    raise ArchiveError("invalid JSON number")


def read_object(path, limit, expected_hash=None):
    require(path.is_file() and not path.is_symlink(), f"missing or unsafe file: {path.name}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, f"file exceeds size limit: {path.name}")
    if expected_hash is not None:
        require(digest(raw) == expected_hash, f"SHA-256 mismatch: {path.name}")
    try:
        item = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as error:
        raise ArchiveError(f"{path.name}: {error}") from error
    require(isinstance(item, dict), f"expected a JSON object: {path.name}")
    return item, raw


def parse_time(value):
    require(isinstance(value, str), "missing receipt or request timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ArchiveError("invalid receipt or request timestamp") from error
    require(result.utcoffset() is not None, "receipt or request timestamp must include offset")
    return result


def records_in_order(archive):
    manifest, _ = read_object(archive / "manifest.json", MAX_META)
    require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1
            and manifest.get("dataset_class") == DATA_CLASS and manifest.get("source_url") == SOURCE
            and manifest.get("execution_eligible") is False
            and manifest.get("exchange_quote_time_verified") is False,
            "unsupported or executable observation manifest")
    entries = manifest.get("records")
    require(isinstance(entries, list) and 0 < len(entries) <= MAX_RECORDS,
            "missing or excessive manifest records")
    records, numbers = [], set()
    for entry in entries:
        require(isinstance(entry, dict), "invalid manifest record")
        name = entry.get("body_file")
        match = re.fullmatch(
            r"snapshot-([1-9][0-9]{0,2})\.raw\.json", name if isinstance(name, str) else ""
        )
        require(match is not None, "unsafe or invalid body filename")
        attempt = int(match[1])
        require(attempt not in numbers, "duplicate observation in manifest")
        numbers.add(attempt)
        receipt_name = f"snapshot-{attempt}.receipt.json"
        require(entry.get("receipt_file") == receipt_name, "unexpected receipt filename")
        for key in ("body_sha256", "receipt_sha256"):
            value = entry.get(key)
            require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value),
                    "missing or invalid manifest hash")
        meta, _ = read_object(archive / receipt_name, MAX_META, entry["receipt_sha256"])
        require(type(meta.get("attempt")) is int and meta["attempt"] == attempt
                and meta.get("http_status") == 200 and meta.get("body_sha256") == entry["body_sha256"]
                and meta.get("source_url") == SOURCE and meta.get("dataset_class") == DATA_CLASS
                and meta.get("execution_eligible") is False
                and all(key in meta and meta[key] is None for key in
                        ("exchange_quote_timestamp", "provider_quote_timestamp", "order_book_sequence")),
                "invalid or ambiguously sourced receipt metadata")
        receipt = parse_time(meta.get("receipt_utc"))
        request = parse_time(meta.get("request_utc"))
        tehran = parse_time(meta.get("receipt_tehran"))
        require(receipt.utcoffset() == request.utcoffset() == timedelta(0)
                and request <= receipt and tehran == receipt
                and tehran.utcoffset() == timedelta(hours=3, minutes=30),
                "inconsistent receipt timestamps")
        records.append((receipt, attempt, archive / name, meta))
    records.sort(key=lambda row: row[0])
    require(len({row[0] for row in records}) == len(records), "duplicate receipt timestamp")
    require([row[1] for row in records] == list(range(1, len(records) + 1)),
            "missing observation or out-of-order capture clock")
    return records


def positive_id(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def run_analysis(script, directory):
    require(script in ANALYSIS_SCRIPTS, "unexpected analysis script")
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(ROOT / script)], cwd=directory,
        capture_output=True, timeout=30,
    )
    require(proc.returncode == 0, f"isolated analysis failed: {script}")


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)


def contract_states(path, meta):
    data, raw = read_object(path, MAX_BODY, meta["body_sha256"])
    records = data.get("instrumentOptMarketWatch")
    require(type(meta.get("byte_count")) is int and len(raw) == meta["byte_count"]
            and type(meta.get("rows")) is int and isinstance(records, list)
            and len(records) == meta["rows"] and records
            and all(isinstance(row, dict) for row in records), "raw body does not match receipt")
    for record in records:
        for field in ("insCode_C", "insCode_P"):
            value = record.get(field)
            require(value is None or value in ("", "0") or (type(value) is int and value == 0)
                    or positive_id(value), f"invalid identifier in {field}")

    # Only analytics modules write, and only in a new scratch directory for this observation.
    with TemporaryDirectory(prefix="tsetmc-shadow-") as scratch:
        scratch = Path(scratch)
        folder = scratch / "data"
        folder.mkdir()
        (folder / "options_market_watch.json").write_bytes(raw)
        write_json(folder / "options_market_watch_meta.json", {
            "status": "موفق", "fetched_at": meta["receipt_tehran"],
            "http_status": 200, "record_count": len(records),
        })
        run_analysis("build_contracts.py", scratch)
        normalized = json.loads((folder / "all_contracts.json").read_bytes())
        states, candidates = {}, []
        for contract in normalized:
            code = contract["ins_code"]
            if code == "0":  # TSETMC may use zero as an absent side of a paired row.
                continue
            require(positive_id(code) and positive_id(contract["underlying_code"]),
                    "invalid contract or underlying identifier")
            require(code not in states, f"duplicate instrument identifier: {code}")
            require(all(type(contract[key]) is int and 0 < contract[key] <= 2**63 - 1
                        for key in ("strike_price", "contract_size")),
                    f"invalid contract multiplier or strike: {code}")
            date = contract["maturity_date"]
            try:
                require(isinstance(date, str) and re.fullmatch(r"[0-9]{8}", date),
                        f"invalid maturity date: {code}")
                datetime.strptime(date, "%Y%m%d")
            except ValueError as error:
                raise ArchiveError(f"invalid maturity date: {code}") from error
            for key in OBSERVED_NUMBERS:
                value = contract[key]
                require(value is None or (type(value) is int and -2**63 <= value <= 2**63 - 1
                                          and (value >= 0 or key == "remaining_days")),
                        f"invalid numeric field {key}: {code}")
            require(all(contract[key] is None or isinstance(contract[key], str)
                        for key in ("symbol", "underlying_symbol")), f"invalid symbol: {code}")

            spot = contract["underlying_closing_price"] or 0
            strike = contract["strike_price"]
            contract["intrinsic_value"] = max(
                0, spot - strike if contract["option_type"] == "CALL" else strike - spot
            )
            candidate, rejection = evaluate_candidate(contract)
            if candidate is not None:
                candidates.append(candidate)
            bid, ask = (contract[key] for key in PRICE_FIELDS)
            prices_ok = bool(bid and ask and bid <= ask)
            states[code] = {
                key: contract[key] for key in ("symbol", "underlying_symbol", *IDENTITY, *OBSERVED_NUMBERS)
            } | {
                "spread_percent": round((ask - bid) / ((bid + ask) / 2) * 100, 6) if prices_ok else None,
                "two_sided": bool(prices_ok and contract["bid_volume"] and contract["ask_volume"]),
                "candidate_eligible": candidate is not None,
                "candidate_rejection": rejection,
                "rank_eligible": False,
                "rank_rejection": None,
                "iv_status": None,
                "iv_failure_reason": None,
                "greeks_assumption_status": None,
            }
        require(states, "snapshot has no identifiable option contracts")
        write_json(folder / "candidates.json", candidates)
        run_analysis("calculate_greeks.py", scratch)
        greeks = json.loads((folder / "greeks_results.json").read_bytes())
        for contract in greeks:
            eligible, reason = check_contract(contract)
            states[contract["ins_code"]].update(
                rank_eligible=eligible, rank_rejection=None if eligible else reason,
                iv_status=contract["iv_status"],
                iv_failure_reason=contract["iv_failure_reason"],
                greeks_assumption_status=contract["greeks_assumption_status"],
            )
    return dict(sorted(states.items()))


def compare(previous, current):
    changes = {}
    for code in sorted(previous.keys() & current.keys()):
        old, new = previous[code], current[code]
        observed = {key: [old[key], new[key]] for key in COMPARE_FIELDS if old[key] != new[key]}
        if observed:
            changes[code] = observed
    return {
        "appeared": sorted(current.keys() - previous.keys()),
        "disappeared": sorted(previous.keys() - current.keys()),
        "changed": changes,
        "price_changes": sum(any(key in delta for key in PRICE_FIELDS) for delta in changes.values()),
        "spread_changes": sum("spread_percent" in delta for delta in changes.values()),
        "liquidity_changes": sum(any(key in delta for key in LIQUIDITY_FIELDS) for delta in changes.values()),
        "eligibility_or_reason_changes": sum(
            any(key in delta for key in ELIGIBILITY_FIELDS) for delta in changes.values()
        ),
    }


def replay(archive):
    observations, previous, identities = [], None, {}
    # Only scheduling metadata is read ahead. Bodies and rules are evaluated one at a time.
    for receipt, attempt, path, meta in records_in_order(Path(archive)):
        current = contract_states(path, meta)
        for code, state in current.items():
            identity = tuple(state[key] for key in IDENTITY)
            require(identities.setdefault(code, identity) == identity,
                    f"contract identity modified between observations: {code}")
        observations.append({
            "attempt": attempt,
            "observed_at_utc": receipt.astimezone(timezone.utc).isoformat(),
            "observed_at_tehran": meta["receipt_tehran"],
            "body_sha256": meta["body_sha256"],
            "exchange_quote_at": None,
            "contract_count": len(current),
            "two_sided_count": sum(row["two_sided"] for row in current.values()),
            "candidate_count": sum(row["candidate_eligible"] for row in current.values()),
            "rank_eligible_count": sum(row["rank_eligible"] for row in current.values()),
            "candidate_rejections": dict(Counter(row["candidate_rejection"] for row in current.values()
                                                 if row["candidate_rejection"] is not None)),
            "rank_rejections": dict(Counter(row["rank_rejection"] for row in current.values()
                                            if row["rank_rejection"] is not None)),
            "since_previous": None if previous is None else compare(previous, current),
            "contracts": current,
        })
        previous = current
    return {
        "schema_version": 1,
        "mode": "shadow_watch",
        "dataset_class": DATA_CLASS,
        "execution_eligible": False,
        "time_basis": "local_http_receipt",
        "price_changes_are_executable_returns": False,
        "rule_sha256": {name: digest((ROOT / name).read_bytes()) for name in MODEL_FILES},
        "observations": observations,
    }


def persian_report(report):
    lines = [
        "پایش سایهٔ آفلاین TSE/IFB — صرفاً مشاهده؛ بدون سفارش یا بازده قابل اجرا",
        "زمان‌ها دریافت HTTP هستند؛ زمان واقعی مظنه تأیید نشده است.",
    ]
    for obs in report["observations"]:
        reasons = "، ".join(f"{label}: {count}" for label, count in obs["rank_rejections"].items())
        lines.append(
            f"{obs['observed_at_tehran']} تهران | {obs['contract_count']} قرارداد، "
            f"{obs['two_sided_count']} دوطرفه، {obs['candidate_count']} نامزد، "
            f"{obs['rank_eligible_count']} واجد رتبه" + (f" | {reasons}" if reasons else "")
        )
        if (changes := obs["since_previous"]) is not None:
            lines.append(
                f"  تغییر قیمت/اسپرد/نقدشوندگی/واجدشرایط یا علت: "
                f"{changes['price_changes']}/{changes['spread_changes']}/{changes['liquidity_changes']}/"
                f"{changes['eligibility_or_reason_changes']} | "
                f"ظاهر/غایب: {len(changes['appeared'])}/{len(changes['disappeared'])}"
            )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="پایش سایهٔ آفلاین اختیار معاملات ایران")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--format", choices=("persian", "json"), default="persian")
    args = parser.parse_args(argv)
    try:
        result = replay(args.archive)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"پایش سایه متوقف شد: {error}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        print(persian_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
