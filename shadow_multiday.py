"""Offline latest-as-of 1/3/7-session observation comparisons, never execution."""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import subprocess
import sys

import shadow_collector as collector
import shadow_watch as watch


HORIZONS = (1, 3, 7)


def session_calendar(path):
    if path is None:
        return None
    data, raw = watch.read_object(path, watch.MAX_META)
    source, dates = data.get("source"), data.get("sessions")
    watch.require(isinstance(source, str) and source.strip() and isinstance(dates, list) and dates,
                  "session calendar requires source and non-empty sessions")
    watch.require(all(isinstance(item, str) and date.fromisoformat(item).isoformat() == item
                      for item in dates) and dates == sorted(set(dates)),
                  "session dates must be unique, canonical YYYY-MM-DD and ascending")
    return {"source": source, "sessions": dates, "sha256": watch.digest(raw),
            "verification": "operator_declared_complete_not_independently_verified"}


def day_horizon(days, covered):
    left, right = covered[0], covered[-1]
    old, new = days[left]["contracts"], days[right]["contracts"]
    results = {}
    for code in sorted(old.keys() | new.keys()):
        if code not in old:
            result = {"status": "absent_at_start"}
        elif code not in new:
            result = {"status": "absent_at_end"}
        elif any(code not in days[day]["contracts"] for day in covered[1:-1]):
            result = {"status": "absent_in_intervening_session"}
        elif not (old[code]["two_sided"] and new[code]["two_sided"]):
            result = {"status": "unusable_endpoint_quotes"}
        else:
            start, end = old[code], new[code]
            bid = end["best_bid"] - start["best_bid"]
            ask = end["best_ask"] - start["best_ask"]
            result = {
                "status": "observational_change",
                "bid_change": bid, "ask_change": ask, "midpoint_change": (bid + ask) / 2,
                "spread_change_percentage_points": round(
                    end["spread_percent"] - start["spread_percent"], 6
                ),
            }
        results[code] = result
    return {"status": "session_coverage_complete", "sessions": covered,
            "start": left, "end": right, "contracts": results,
            "status_counts": dict(Counter(row["status"] for row in results.values()))}


def horizons_at_latest(days, calendar):
    if not calendar:
        return None, {str(h): {"status": "session_calendar_required"} for h in HORIZONS}
    sessions = calendar["sessions"]
    latest = max((index for index, day in enumerate(sessions) if day in days), default=None)
    results = {}
    for horizon in HORIZONS:
        if latest is None:
            result = {"status": "no_observed_listed_session"}
        elif latest < horizon:
            result = {"status": "insufficient_calendar_lookback"}
        else:
            covered = sessions[latest - horizon:latest + 1]
            missing = [day for day in covered if day not in days]
            result = ({"status": "missing_session_observations", "sessions": covered,
                       "missing_sessions": missing} if missing else day_horizon(days, covered))
        results[str(horizon)] = result
    return sessions[latest] if latest is not None else None, results


def build_report(stream, *, legacy=None, calendar=None, expected_head=None,
                 dataset_class=watch.DATA_CLASS):
    watch.require(dataset_class == watch.DATA_CLASS or (dataset_class == collector.SYNTHETIC and legacy is None),
                  "cannot mix synthetic and real observations")
    records = []
    if legacy is not None:
        records.extend((f"legacy:{attempt}", path, meta)
                       for _, attempt, path, meta in watch.records_in_order(legacy))
    if stream.exists() or stream.is_symlink():
        with collector.locked(stream, exclusive=False):
            current = collector.read_stream(stream, dataset_class)
    else:
        current = []
    head = current[-1]["head_sha256"] if current else None
    watch.require(expected_head is None or head == expected_head, "stream head does not match pinned SHA-256")
    records.extend((f"manual:{meta['sequence']}", meta["path"], meta) for meta in current)

    dates, identities, seen, previous = {}, {}, set(), None
    for capture_id, path, meta in records:
        received = collector.utc(meta["receipt_utc"])
        started = collector.utc(meta["request_utc"])
        watch.require(previous is None or (received > previous and
                      (not capture_id.startswith("manual:") or started > previous)),
                      "legacy/manual overlap or out-of-order receipt")
        body_hash = meta["body_sha256"]
        watch.require(not capture_id.startswith("manual:") or body_hash not in seen,
                      "manual observation repeats a legacy body")
        seen.add(body_hash)
        previous = received
        states = watch.contract_states(path, meta)
        for code, row in states.items():
            fingerprint = tuple(row[key] for key in watch.IDENTITY)
            watch.require(identities.setdefault(code, fingerprint) == fingerprint,
                          f"contract identity modified between observations: {code}")
        day = watch.parse_time(meta["receipt_tehran"]).date().isoformat()
        count = dates.get(day, {}).get("capture_count", 0) + 1
        dates[day] = {
            "capture_id": capture_id, "capture_count": count, "receipt_tehran": meta["receipt_tehran"],
            "receipt_utc": meta["receipt_utc"], "exchange_quote_at": None, "body_sha256": body_hash,
            "contract_count": len(states), "rank_eligible_count": sum(row["rank_eligible"] for row in states.values()),
            "candidate_rejections": dict(Counter(row["candidate_rejection"] for row in states.values()
                                                 if row["candidate_rejection"] is not None)),
            "rank_rejections": dict(Counter(row["rank_rejection"] for row in states.values()
                                            if row["rank_rejection"] is not None)),
            "contracts": states,
        }
    latest, results = horizons_at_latest(dates, calendar)
    return {
        "schema_version": 1, "mode": "shadow_multiday", "dataset_class": dataset_class,
        "source_url": watch.SOURCE if dataset_class == watch.DATA_CLASS else None,
        "execution_eligible": False, "exchange_quote_time_verified": False,
        "price_changes_are_executable_returns": False,
        "notice": "Quote observations only; not executable prices, fills, profits or recommendations.",
        "day_basis": "latest_local_http_receipt_per_Tehran_date_not_exchange_close",
        "model_note": "Existing experimental Greeks and IV; null status means not evaluated.",
        "rule_sha256": {name: watch.digest((watch.ROOT / name).read_bytes()) for name in watch.MODEL_FILES},
        "manual_head_sha256": head, "session_calendar": calendar,
        "observed_dates_outside_calendar": sorted(set(dates) - set(calendar["sessions"])) if calendar else None,
        "as_of_listed_session": latest, "days": dates, "horizons": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=Path, help="local JSON list of consecutive exchange sessions and source")
    parser.add_argument("--no-legacy", action="store_true", help="exclude the original read-only batch")
    parser.add_argument("--expect-head", help="require this exact latest manual seal SHA-256")
    args = parser.parse_args(argv)
    legacy = watch.DEFAULT_ARCHIVE if not args.no_legacy and watch.DEFAULT_ARCHIVE.exists() else None
    try:
        report = build_report(collector.STREAM, legacy=legacy, calendar=session_calendar(args.sessions),
                              expected_head=args.expect_head)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Multiday report stopped: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
