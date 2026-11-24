"""Read-only archive cache, candidate queries and daily observation comparisons."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import islice
import math
from pathlib import Path
import re
import stat
import subprocess
import unicodedata
from urllib.parse import parse_qs
from zoneinfo import ZoneInfoNotFoundError

import combine_analysis as combined
import fetch_underlying_history as fetch
import greeks_evidence_registry as evidence
import shadow_collector as collector
import shadow_multiday
import shadow_watch as watch


PAGE_SIZE = 20
MAX_STREAM_RECORDS = 256
PERSIAN_LETTERS = str.maketrans("يك", "یک")
SORTS = {
    "spread": ("spread_percent", "اسپرد"), "symbol": ("symbol", "نماد"),
    "bid": ("best_bid", "خرید"), "ask": ("best_ask", "فروش"),
    "trades": ("trade_count", "تعداد معامله"), "days": ("remaining_days", "روز مانده"),
}
STATUSES = {"passed": "عبور از غربال", "rejected": "رد در غربال", "all": "همهٔ قراردادها"}
ARCHIVE_ERRORS = (OSError, ValueError, subprocess.TimeoutExpired)
COMBINED_CAPTURE = "manual:2"
COMBINED_ERRORS = (*ARCHIVE_ERRORS, ZoneInfoNotFoundError)
COMBINED_VIEWS = {"options": "قراردادها", "underlyings": "دارایی‌های پایه", "diagnostics": "تشخیص داده"}
COMBINED_STATUSES = {"RESEARCH_ONLY": "فقط پژوهش؛ نه سیگنال", "REJECTED": "ردشده", "UNKNOWN": "شاهد ناکافی"}
COMBINED_SCREENING = {"passed": "عبور از غربال", "rejected": "رد در غربال", "unknown": "ارزیابی نشده"}
COMBINED_LIQUIDITY = {
    "observed": "دوطرفه، بدون ایراد ثبت‌شده", "limited": "محدود یا ناسازگار", "unknown": "نامعلوم",
}
COMBINED_CODE_FILES = (*watch.MODEL_FILES, "combine_analysis.py", "market_data_quality.py",
                       "analyze_underlying_trends_v2.py", "greeks_evidence_registry.py",
                       "shadow_watch.py", "shadow_collector.py")


def text_key(value):
    return unicodedata.normalize("NFKC", value or "").strip().casefold().translate(PERSIAN_LETTERS)


def query_values(raw, fields):
    if len(raw) > 2048 or not raw.isascii() or re.search(r"%(?![0-9a-fA-F]{2})", raw):
        raise ValueError("invalid query encoding or length")
    values = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=len(fields), errors="strict")
    if values.keys() - fields.keys() or any(len(v) != 1 for v in values.values()):
        raise ValueError("unknown or repeated query field")
    return {key: value[0] for key, value in values.items()}


def validate_search_text(value):
    if len(value) > 80 or any(unicodedata.category(c).startswith("C") and c not in "\u200c\u200d" for c in value):
        raise ValueError("invalid search text")


def query_page(value):
    if not re.fullmatch(r"[1-9][0-9]{0,4}", value):
        raise ValueError("invalid page")
    return int(value)


@dataclass(frozen=True)
class Query:
    q: str = ""
    kind: str = ""
    underlying: str = ""
    spread: str = ""
    status: str = "passed"
    sort: str = "spread"
    order: str = "asc"
    page: int = 1

    @classmethod
    def parse(cls, raw):
        values = query_values(raw, cls.__dataclass_fields__)
        for key in ("q", "underlying"):
            validate_search_text(values.get(key, ""))
        query = cls(**(values | {"page": query_page(values.get("page", "1"))}))
        if (query.kind not in ("", "CALL", "PUT") or query.status not in STATUSES
                or query.sort not in SORTS or query.order not in ("asc", "desc")):
            raise ValueError("invalid filter or sort")
        if query.spread and (not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,6})?", query.spread)
                             or Decimal(query.spread) > 200):
            raise ValueError("invalid spread limit")
        return query


@dataclass(frozen=True)
class CombinedQuery:
    q: str = ""
    kind: str = ""
    status: str = ""
    screening: str = ""
    liquidity: str = ""
    expiry: str = ""
    view: str = "options"
    page: int = 1

    @classmethod
    def parse(cls, raw):
        values = query_values(raw, cls.__dataclass_fields__)
        validate_search_text(values.get("q", ""))
        query = cls(**(values | {"page": query_page(values.get("page", "1"))}))
        if (query.view not in COMBINED_VIEWS or query.kind not in ("", "CALL", "PUT")
                or query.status not in ("", *COMBINED_STATUSES)
                or query.screening not in ("", *COMBINED_SCREENING)
                or query.liquidity not in ("", *COMBINED_LIQUIDITY)):
            raise ValueError("invalid combined research filter")
        if query.expiry and (not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", query.expiry)
                             or date.fromisoformat(query.expiry).isoformat() != query.expiry):
            raise ValueError("invalid expiry filter")
        return query


def combined_screening(option):
    screened = (option.get("screening") or {}).get("candidate_eligible")
    return "passed" if screened is True else "rejected" if screened is False else "unknown"


def combined_liquidity(option):
    quote, issues = option.get("quote_class"), option.get("liquidity_issues")
    if quote in (None, "missing") or issues is None:
        return "unknown"
    return "observed" if quote == "two_sided" and not issues else "limited"


def combined_rows(report, query):
    """Select displayed rows only; no screening or research calculation here."""
    if query.view == "underlyings":
        rows = sorted(report["underlyings"].items())
    elif query.view == "diagnostics":
        rows = sorted(report["diagnostics"], key=lambda row: (row["raw_row"], row["side"]))
    else:
        term, expiry = text_key(query.q), query.expiry.replace("-", "")
        rows = []
        for row in report["options"]:
            if ((query.kind and query.kind != row["option_type"])
                    or (query.status and query.status != row["status"])
                    or (query.screening and query.screening != combined_screening(row))
                    or (query.liquidity and query.liquidity != combined_liquidity(row))
                    or (expiry and expiry != row.get("maturity_date"))):
                continue
            if term:
                base = report["underlyings"][row["underlying_code"]]
                names = (row["ins_code"], row["underlying_code"], row.get("symbol"),
                         row.get("underlying_symbol"), *base["symbols"])
                if not any(term in text_key(name) for name in names):
                    continue
            rows.append(row)
        rows.sort(key=lambda row: row["ins_code"])
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    number = min(query.page, pages)
    return rows[(number - 1) * PAGE_SIZE:number * PAGE_SIZE], total, number, pages


def numeric(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def select_rows(contracts, query):
    q, base = text_key(query.q), text_key(query.underlying)
    limit = Decimal(query.spread) if query.spread else None
    known, missing = [], []
    key = SORTS[query.sort][0]
    for code, row in contracts.items():
        passed = row["candidate_eligible"]
        if query.status != "all" and passed != (query.status == "passed"):
            continue
        if query.kind and query.kind != row["option_type"]:
            continue
        underlying = text_key(row.get("underlying_symbol"))
        if base and base != underlying:
            continue
        if q and not any(q in v for v in (text_key(row["symbol"]), code, underlying, row["underlying_code"])):
            continue
        spread = row["spread_percent"]
        if limit is not None and (not numeric(spread) or Decimal(str(spread)) > limit):
            continue
        value = text_key(row[key]) if key == "symbol" else row[key]
        has_value = bool(value) if key == "symbol" else numeric(value)
        (known if has_value else missing).append((code, row, value))
    # Stable tie break in either direction; missing values always last.
    known.sort(key=lambda item: item[0])
    known.sort(key=lambda item: item[2], reverse=query.order == "desc")
    missing.sort(key=lambda item: item[0])
    ordered = known + missing
    total = len(ordered)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(query.page, pages)
    return [(code, row) for code, row, _ in ordered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]], total, page, pages


def compare_contract(code, old, new):
    if old is None:
        return "absent", {}
    if (not watch.positive_id(code) or not watch.positive_id(old.get("underlying_code"))
            or old.get("option_type") not in ("CALL", "PUT") or any(
            old.get(key) is None or type(old[key]) is not type(new.get(key)) or old[key] != new[key]
            for key in watch.IDENTITY)):
        return "identity", {}
    for row in (old, new):
        bid, ask, bid_depth, ask_depth = (row.get(k) for k in (*watch.PRICE_FIELDS, "bid_volume", "ask_volume"))
        spread = row.get("spread_percent")
        if (not row.get("two_sided")
                or not all(type(v) is int and v > 0 for v in (bid, ask, bid_depth, ask_depth))
                or bid > ask or not numeric(spread) or not 0 <= spread < 200):
            return "quote", {}
    changes = {key: new[key] - old[key] for key in (*watch.PRICE_FIELDS, "bid_volume", "ask_volume")}
    changes["spread_percent"] = round(new["spread_percent"] - old["spread_percent"], 6)
    for key in ("trade_count", "trade_volume"):
        if all(type(row.get(key)) is int and row[key] >= 0 for row in (old, new)):
            changes[key] = new[key] - old[key]
    return "observed", changes


def daily_comparison(day, latest, days):
    current_day = date.fromisoformat(day)
    prior_day = (current_day - timedelta(days=1)).isoformat() if current_day > date.min else None
    older = days.get(prior_day)
    if older is None or latest["body_sha256"] == older["body_sha256"]:
        return prior_day, None, {}, {}
    old, new = older["contracts"], latest["contracts"]
    results = {code: compare_contract(code, old.get(code), row) for code, row in new.items()}
    counts = Counter(status for status, _ in results.values())
    counts["disappeared"] = len(old.keys() - new.keys())
    for _, changes in results.values():
        counts["price_changed"] += any(changes.get(k, 0) for k in watch.PRICE_FIELDS)
        counts["spread_changed"] += bool(changes.get("spread_percent"))
        counts["liquidity_changed"] += any(changes.get(k, 0) for k in watch.LIQUIDITY_FIELDS)
        counts["trade_counters_available"] += "trade_count" in changes and "trade_volume" in changes
    return prior_day, older["receipt_tehran"], results, counts


def stamp(path, check, *, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    watch.require(check(info.st_mode), "unsafe archive input type")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)


def entries(root, limit):
    paths = sorted(islice(root.iterdir(), limit + 1))
    watch.require(len(paths) <= limit, "dashboard archive limit exceeded")
    return paths


def manual_file_stamps(stream):
    with collector.locked(stream, exclusive=False):
        for path in entries(stream, MAX_STREAM_RECORDS):
            watch.require(collector.RECORD.fullmatch(path.name), "unexpected archive entry")
            yield path.name, stamp(path, stat.S_ISDIR)
            for child in entries(path, len(collector.FILES)):
                yield child.name, stamp(child, stat.S_ISREG)


def add_csv_overlay_args(parser):
    for flag, key in combined.CSV_OVERLAY_FLAGS.items():
        parser.add_argument(flag, dest="csv_fields", action=combined.CSVOverlayField,
                            type=Path if key == "source" else str,
                            choices=fetch.CSV_CLOSE_POLICIES if key == "close_policy" else None,
                            help="منبع CSV فقط برای پژوهش کل بازار در شروع سرور" if key == "source" else None)


def report_overlays(report):
    if "history_overlays" in report:
        return report["history_overlays"]
    if entry := report.get("history_overlay"):
        watch.require(isinstance(entry, dict), "unexpected combined CSV provenance")
        return {entry.get("ins_code"): entry}
    return {}


class ArchiveCache:
    """Stat the bounded archive on GET; replay only on a changed generation."""

    def __init__(self, stream=collector.STREAM, legacy=watch.DEFAULT_ARCHIVE):
        self.stream, self.legacy = stream, legacy
        self.key = None
        self.report = None

    def signature(self):
        legacy = stamp(self.legacy, stat.S_ISDIR, optional=True)
        stream = stamp(self.stream, stat.S_ISDIR, optional=True)
        result = [legacy, stream]
        if legacy is not None:
            result.extend((p.name, stamp(p, stat.S_ISREG)) for p in entries(self.legacy, 2 * watch.MAX_RECORDS + 1))
        if stream is not None:
            result.extend(manual_file_stamps(self.stream))
        result.extend(stamp(watch.ROOT / name, stat.S_ISREG) for name in watch.MODEL_FILES)
        return tuple(result)

    def get(self):
        """Return (report, failed); never serve stale data after a validation error."""
        try:
            key = self.signature()
        except ARCHIVE_ERRORS:
            self.key, self.report = None, None
            return None, True
        if key != self.key:
            self.key, self.report = None, None
            try:
                report = shadow_multiday.build_report(self.stream, legacy=self.legacy if key[0] else None)
            except ARCHIVE_ERRORS as error:
                # A concurrent writer can briefly hold the lock even if it doesn't append.
                if not (isinstance(error, watch.ArchiveError) and isinstance(error.__cause__, BlockingIOError)):
                    self.key = key
                return None, True
            try:
                watch.require(key == self.signature(), "archive changed during replay")
            except ARCHIVE_ERRORS:
                return None, True
            day = max(report["days"], default=None)
            report["dashboard_comparison"] = daily_comparison(
                day, report["days"][day], report["days"]) if day else None
            self.key, self.report = key, report
        return self.report, self.report is None


class CombinedCache:
    """One in-memory report per server; default mode is lazy and CSV mode is startup-pinned."""

    def __init__(self, stream=collector.STREAM, history_path=watch.ROOT / "data/underlying_history.json", *,
                 csv_overlays=()):
        self.stream, self.history_path = stream, history_path
        self._overlay_specs = tuple(sorted((tuple(sorted(spec.items())) for spec in csv_overlays),
                                           key=lambda spec: dict(spec)["code"]))
        self.key = self.report = None
        try:
            self.code_key = self.code_signature()
        except COMBINED_ERRORS:
            self.code_key = None

    def code_signature(self):
        files = COMBINED_CODE_FILES + (("fetch_underlying_history.py",) if self._overlay_specs else ())
        return tuple(stamp(watch.ROOT / name, stat.S_ISREG) for name in files)

    def csv_signature(self):
        fingerprints, size = [], 0
        for spec in self._overlay_specs:
            config = dict(spec)
            expected = config["source_sha256"]
            watch.require(fetch.is_sha256(expected), "invalid CSV SHA-256")
            with fetch.pinned_path(config["source"]) as source:
                raw, version = source.read(fetch.MAX_CSV_BYTES)
            size += len(raw)
            watch.require(size <= watch.MAX_BODY, "combined CSV inputs exceed size limit")
            actual = watch.digest(raw)
            watch.require(actual == expected, "CSV SHA-256 mismatch")
            fingerprints.append((spec, actual, version))
        return tuple(fingerprints)

    def signature(self):
        # Imported Python modules cannot be safely hot-reloaded with the replay scripts.
        watch.require(self.code_key is not None and self.code_key == self.code_signature(),
                      "restart required after research code change")
        return (COMBINED_CAPTURE, self.csv_signature(), stamp(self.stream, stat.S_ISDIR),
                *manual_file_stamps(self.stream), stamp(self.history_path, stat.S_ISREG, optional=True),
                stamp(evidence.REGISTRY, stat.S_ISREG))

    def validate_overlays(self, report):
        overlays = report_overlays(report)
        specs = [dict(spec) for spec in self._overlay_specs]
        watch.require(isinstance(overlays, dict) and overlays.keys() == {spec["code"] for spec in specs},
                      "unexpected combined CSV provenance")
        for spec in specs:
            code = spec["code"]
            entry = overlays[code]
            csv = entry.get("csv") if isinstance(entry, dict) else None
            watch.require(isinstance(csv, dict) and code in report["underlyings"] and entry.get("ins_code") == code
                          and entry.get("status") == "RESEARCH_ONLY"
                          and entry.get("point_in_time_verified") is False
                          and entry.get("execution_eligible") is False
                          and csv.get("body_sha256") == spec["source_sha256"]
                          and csv.get("instrument_page_url") == spec["instrument_page_url"]
                          and csv.get("source_format") == "tsetmc_manual_csv"
                          and entry.get("close_policy", "strict") == spec.get("close_policy", "strict"),
                          "unexpected combined CSV provenance")
        if specs:
            watch.require(all(isinstance(row, dict) and row.get("execution_eligible") is False
                              for row in report["options"]),
                          "unexpected executable research row")

    def get(self):
        try:
            key = self.signature()
            if self.report is not None and key == self.key:
                return self.report
            self.key = self.report = None
            report = combined.build_market_research_report(
                COMBINED_CAPTURE, stream=self.stream, legacy_archive=watch.DEFAULT_ARCHIVE, history_path=self.history_path,
                **({"csv_overlays": tuple(dict(spec) for spec in self._overlay_specs)} if self._overlay_specs else {}),
            )
            watch.require(isinstance(report, dict) and report.get("schema_version") == 2
                          and all(isinstance(report.get(key), dict)
                                  for key in ("capture", "summary", "underlyings", "reconciliation"))
                          and all(isinstance(report.get(key), list)
                                  for key in ("options", "diagnostics", "limitations_fa"))
                          and report.get("research_only") is True
                          and report.get("execution_eligible") is False and report.get("point_in_time_verified") is False
                          and report["capture"].get("capture_id") == COMBINED_CAPTURE
                          and report.get("greeks_assumption_status") == "experimental_unverified"
                          and report.get("risk") == {"status": "UNKNOWN", "max_quantity": None},
                          "unexpected combined research report")
            self.validate_overlays(report)
            watch.require(key == self.signature(), "combined research inputs changed during replay")
        except COMBINED_ERRORS:
            self.key = self.report = None
            return None
        self.key, self.report = key, report
        return report
