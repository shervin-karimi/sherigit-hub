"""Read-only archive cache, candidate queries and daily observation comparisons."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import islice
import math
import re
import stat
import subprocess
import unicodedata
from urllib.parse import parse_qs

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


def text_key(value):
    return unicodedata.normalize("NFKC", value or "").strip().casefold().translate(PERSIAN_LETTERS)


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
        if len(raw) > 2048 or not raw.isascii() or re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise ValueError("invalid query encoding or length")
        values = parse_qs(raw, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=8, errors="strict")
        if (values.keys() - cls.__dataclass_fields__.keys()
                or any(len(v) != 1 for v in values.values())):
            raise ValueError("unknown or repeated query field")
        for value in (values.get(key, [""])[0] for key in ("q", "underlying")):
            if len(value) > 80 or any(unicodedata.category(c).startswith("C") and c not in "\u200c\u200d"
                                      for c in value):
                raise ValueError("invalid search text")
        query = cls(**{key: value[0] for key, value in values.items() if key != "page"})
        if (query.kind not in ("", "CALL", "PUT") or query.status not in STATUSES
                or query.sort not in SORTS or query.order not in ("asc", "desc")):
            raise ValueError("invalid filter or sort")
        if query.spread and (not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,6})?", query.spread)
                             or Decimal(query.spread) > 200):
            raise ValueError("invalid spread limit")
        page, = values.get("page", ["1"])
        if not re.fullmatch(r"[1-9][0-9]{0,4}", page):
            raise ValueError("invalid page")
        return cls(**(vars(query) | {"page": int(page)}))


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
            with collector.locked(self.stream, exclusive=False):
                for path in entries(self.stream, MAX_STREAM_RECORDS):
                    watch.require(collector.RECORD.fullmatch(path.name), "unexpected archive entry")
                    result.append((path.name, stamp(path, stat.S_ISDIR)))
                    for child in entries(path, len(collector.FILES)):
                        result.append((child.name, stamp(child, stat.S_ISREG)))
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
