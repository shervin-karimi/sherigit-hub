"""Offline archive inventory, daily-history import, and staged acquisition."""

import argparse
from contextlib import contextmanager, ExitStack
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from io import StringIO
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import unicodedata
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import analyze_underlying_trends_v2 as trends
import combine_analysis as combined
import market_data_quality as quality
import shadow_collector as collector
import shadow_watch as watch


BASE_URL = "https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList"
INSTRUMENT_PAGE = "https://www.tsetmc.com/instInfo/"
OUTPUT_FILE = watch.ROOT / "data/underlying_history.json"
RECORD_LIMIT = 40
REQUIRED_HISTORY = 21
REQUEST_TIMEOUT = 30
MIN_DELAY = 60
MAX_REQUESTS = 32
MAX_RESPONSE = 256 * 1024
MAX_CSV_BYTES = 1024 * 1024
MAX_EXACT_OHLCV = 2**53
CSV_HEADER = (
    "<TICKER>", "<DTYYYYMMDD>", "<FIRST>", "<HIGH>", "<LOW>", "<CLOSE>",
    "<VALUE>", "<VOL>", "<OPENINT>", "<PER>", "<OPEN>", "<LAST>",
)
CSV_PRICE_COLUMNS = ("<FIRST>", "<HIGH>", "<LOW>", "<CLOSE>", "<OPEN>", "<LAST>")
PUBLICATION_DISABLED = (
    "انتشار تاریخچه غیرفعال است: ایمنی در برابر نویسندهٔ هم‌زمان حل نشده است "
    "(publication disabled: unresolved concurrent-writer safety). از پیش‌نمایش بدون --apply استفاده کنید."
)


def capture_inventory(capture_id, *, stream=collector.STREAM, legacy_archive=watch.DEFAULT_ARCHIVE):
    match = re.fullmatch(r"(manual|legacy):([1-9][0-9]{0,7})", capture_id)
    watch.require(match is not None, "شناسهٔ دقیق ثبت لازم است: manual:N یا legacy:N")
    family, number = match[1], int(match[2])

    def inventory(meta, path, receipt_hash):
        document, raw = watch.read_object(path, watch.MAX_BODY, meta["body_sha256"])
        rows = document.get("instrumentOptMarketWatch")
        watch.require(isinstance(rows, list) and rows and type(meta.get("rows")) is int
                      and len(rows) == meta["rows"] and type(meta.get("byte_count")) is int
                      and len(raw) == meta["byte_count"], "body does not match receipt")
        _, bases, diagnostics, _ = quality.inventory_contracts(rows)
        return {
            "capture_id": capture_id, "receipt_tehran": meta["receipt_tehran"],
            "body_sha256": meta["body_sha256"], **receipt_hash,
            "diagnostics": diagnostics,
        }, bases

    if family == "manual":
        with collector.locked(stream, exclusive=False):
            meta = next((row for row in collector.read_stream(stream) if row["sequence"] == number), None)
            watch.require(meta is not None, "ثبت درخواستی پیدا نشد")
            return inventory(meta, meta["path"], {"seal_sha256": meta["head_sha256"]})
    watch.require(legacy_archive.is_dir() and not legacy_archive.is_symlink(), "unsafe legacy archive")
    manifest_path = legacy_archive / "manifest.json"
    _, manifest = watch.read_object(manifest_path, watch.MAX_META)
    selected = next((row for row in watch.records_in_order(legacy_archive) if row[1] == number), None)
    watch.require(selected is not None, "ثبت درخواستی پیدا نشد")
    _, _, path, meta = selected
    result = inventory(meta, path, {"manifest_sha256": watch.digest(manifest)})
    watch.read_object(manifest_path, watch.MAX_META, watch.digest(manifest))
    return result


def preview(capture_id, *, stream=collector.STREAM, legacy_archive=watch.DEFAULT_ARCHIVE, history_path=OUTPUT_FILE):
    capture, bases = capture_inventory(capture_id, stream=stream, legacy_archive=legacy_archive)
    indexed = combined.history_index(history_path)
    receipt = watch.parse_time(capture["receipt_tehran"])
    instruments = {}
    for code, names in sorted(bases.items()):
        history = combined.indexed_history(indexed, code, receipt)
        instruments[code] = {
            "symbols": sorted(names),
            "instrument_type": "UNKNOWN", "instrument_type_verified": False,
            "history_entry_exists": code in indexed[2],
            "technical_analysis_status": "AVAILABLE" if history["status"] == "RESEARCH_ONLY" else "NOT_AVAILABLE",
            "reason_code": history["reason_code"], "reason_fa": history["reason_fa"],
            "valid_closes_before_cutoff": (history["history"] or {}).get("valid_close_count"),
        }
    return {
        "capture": capture, "history_sha256": indexed[0], "instruments": instruments,
        "available_count": sum(row["technical_analysis_status"] == "AVAILABLE" for row in instruments.values()),
        "missing_ids": [code for code, row in instruments.items()
                        if row["reason_code"] in ("history_id_not_found", "history_file_missing")],
        "point_in_time_verified": False, "execution_eligible": False,
        "limitations": ["Instrument type is not verified by this archive.",
                        "HTTP receipt is not quote time; units, adjustments, completeness and publication are unknown."],
    }


def finite_float(value):
    result = float(value)
    watch.require(math.isfinite(result), "invalid JSON number")
    return result


def exact_decimal(value):
    original = Decimal(value)
    compatible = finite_float(original)
    # Existing JSON history readers use float. Refuse decimal values whose
    # numerical value would change when emitted by the standard JSON encoder.
    watch.require(Decimal(str(compatible)) == original, "decimal precision cannot be preserved")
    return original


def decode_object(raw, limit, *, parse_float=finite_float):
    watch.require(len(raw) <= limit, "JSON exceeds size limit")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=watch.unique_object,
                            parse_constant=watch.invalid_constant, parse_float=parse_float)
    except (ValueError, RecursionError, InvalidOperation) as error:
        raise ValueError(f"invalid JSON response or history document: {error}") from error
    watch.require(isinstance(result, dict), "expected a JSON object")
    return result


def validate_history(raw, code, fetched_at):
    """Normalize only ID/date representation and attach quality; never fill gaps."""
    watch.require(watch.positive_id(code), "invalid stable instrument ID")
    receipt_day = watch.parse_time(fetched_at).astimezone(ZoneInfo("Asia/Tehran")).date()
    rows = decode_object(raw, MAX_RESPONSE, parse_float=exact_decimal).get("closingPriceDaily")
    watch.require(isinstance(rows, list) and 0 < len(rows) <= RECORD_LIMIT, "invalid closingPriceDaily array")
    # These JSON-ready rows retain the checked decimal values on serialization;
    # all comparisons below use the exact original int/Decimal values instead.
    output_rows = decode_object(raw, MAX_RESPONSE)["closingPriceDaily"]
    seen, prepared = set(), []
    for row, output_row in zip(rows, output_rows):
        watch.require(isinstance(row, dict), "invalid daily record")
        raw_id = row.get("insCode")
        watch.require(type(raw_id) in (str, int) and str(raw_id) == code, "daily record insCode mismatch")
        day = trends.session_date(row.get("dEven"))
        watch.require(day <= receipt_day and day not in seen, "future or duplicate session date")
        seen.add(day)
        values = [row.get(field) for field in ("pClosing", "priceFirst", "priceMin", "priceMax", "qTotTran5J")]
        watch.require(all(type(value) in (int, Decimal) for value in values), "invalid numeric OHLCV")
        close, first, low, high, volume = values
        watch.require(close > 0 and volume >= 0, "invalid numeric OHLCV")
        ohlc = 0 < low <= close <= high and low <= first <= high
        # A known no-trade observation carries an all-zero range; its prior close
        # is preserved but must not become an active session.
        watch.require(ohlc or (volume == 0 and first == low == high == 0), "inconsistent OHLC")
        watch.require(all(-MAX_EXACT_OHLCV <= value <= MAX_EXACT_OHLCV for value in values),
                      "OHLCV exceeds exact numeric range")
        prepared.append(dict(output_row, insCode=code, dEven=int(day.strftime("%Y%m%d")), _quality={
            "close_valid": True, "volume_valid": volume > 0, "ohlc_valid": ohlc,
        }))
    prepared.sort(key=lambda row: row["dEven"], reverse=True)
    active = sum(row["_quality"]["volume_valid"] for row in prepared)
    watch.require(active >= REQUIRED_HISTORY, "fewer than 21 active sessions in provider response")
    return prepared


def csv_number(value, *, integer=False):
    watch.require(len(value) <= 64 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value),
                  "invalid CSV number")
    number = Decimal(value)
    watch.require(number <= MAX_EXACT_OHLCV, "CSV number exceeds exact numeric range")
    if number == int(number):
        return int(number), number
    watch.require(not integer, "CSV volume or counter is not an integer")
    return float(exact_decimal(value)), number


CSV_CLOSE_POLICIES = ("strict", "exclude-close-outside-traded-range")


def validate_csv_history(raw, source_sha256, code, *, received_at, close_policy="strict"):
    """Validate and normalize a single-instrument export, preserving original cells."""
    watch.require(len(raw) <= MAX_CSV_BYTES, "CSV exceeds size limit")
    watch.require(is_sha256(source_sha256) and watch.digest(raw) == source_sha256,
                  "provider payload SHA-256 mismatch")
    watch.require(watch.positive_id(code), "invalid stable instrument ID")
    watch.require(close_policy in CSV_CLOSE_POLICIES, "invalid CSV close policy")
    receipt_day = (watch.parse_time(received_at).astimezone(ZoneInfo("Asia/Tehran")).date()
                   if received_at is not None else None)
    reader = csv.reader(StringIO(raw.decode("utf-8"), newline=""), strict=True)
    rows, seen, ticker = [], set(), None
    try:
        watch.require(tuple(next(reader, ())) == CSV_HEADER, "unexpected CSV header")
        for cells in reader:
            if not cells and reader.line_num == 2:
                continue  # Optional export separator immediately after the header.
            watch.require(len(cells) == len(CSV_HEADER) and all(not cell or cell.isprintable() for cell in cells),
                          f"invalid CSV row at line {reader.line_num}")
            source = dict(zip(CSV_HEADER, cells))
            fields = {name: value.strip(" ") for name, value in source.items()}
            watch.require(all(fields.values()), f"missing CSV field at line {reader.line_num}")
            watch.require(fields["<PER>"] == "D" and (ticker is None or fields["<TICKER>"] == ticker),
                          "inconsistent CSV ticker or period")
            ticker = fields["<TICKER>"]
            try:
                day = trends.session_date(fields["<DTYYYYMMDD>"])
            except ValueError as error:
                raise ValueError(f"invalid CSV YYYYMMDD session date at line {reader.line_num}") from error
            watch.require(day not in seen and (receipt_day is None or day <= receipt_day),
                          "future or duplicate CSV session date")
            seen.add(day)
            numbers = {
                key: csv_number(fields[key], integer=key in ("<VOL>", "<OPENINT>"))
                for key in (*CSV_PRICE_COLUMNS, "<VOL>", "<VALUE>", "<OPENINT>")
            }
            first, high, low, close, previous, last = (numbers[key][1] for key in CSV_PRICE_COLUMNS)
            volume = numbers["<VOL>"][0]
            watch.require(close > 0, "invalid CSV closing price")
            active = volume > 0
            anomaly = None
            if active:
                watch.require(0 < low <= min(first, last) <= max(first, last) <= high,
                              "inconsistent active CSV OHLC/last")
                if not low <= close <= high:
                    watch.require(close_policy != "strict", "inconsistent active CSV OHLC/last")
                    anomaly = "close_above_traded_high" if close > high else "close_below_traded_low"
            normalized = {field: numbers[key][0] for field, key in (
                ("priceFirst", "<FIRST>"), ("priceMax", "<HIGH>"), ("priceMin", "<LOW>"),
                ("pClosing", "<CLOSE>"), ("pDrCotVal", "<LAST>"), ("qTotTran5J", "<VOL>"),
                ("priceYesterday", "<OPEN>"),
            )}
            rows.append(normalized | {
                "dEven": int(fields["<DTYYYYMMDD>"]), "insCode": code,
                "_source_fields": source,
                "_quality": {"close_valid": not anomaly, "volume_valid": active, "ohlc_valid": active and not anomaly,
                             "zero_volume_unusual_prices": not active and (
                                 any((first, high, low)) or previous != close),
                             **({"close_anomaly": anomaly} if anomaly else {})},
            })
    except csv.Error as error:
        raise ValueError(f"invalid CSV syntax at line {reader.line_num}") from error
    watch.require(sum(row["_quality"]["ohlc_valid"] for row in rows) >= REQUIRED_HISTORY,
                  "fewer than 21 active sessions in CSV" + ("" if close_policy == "strict" else " after close policy"))
    rows.sort(key=lambda row: row["dEven"], reverse=True)
    return rows, ticker


def history_ids(document, *, preview_unknown_csv_code=None):
    symbols = document.get("symbols")
    watch.require(isinstance(symbols, dict) and isinstance(document.get("metadata"), dict),
                  "invalid existing history schema")
    ids = set()
    for name, item in symbols.items():
        code = item.get("code") if isinstance(item, dict) else None
        watch.require(name.strip() and watch.positive_id(code) and code not in ids,
                      "invalid or ambiguous existing instrument identity")
        ids.add(code)
        if not (code == preview_unknown_csv_code and trends.unknown_csv_receipt(item)):
            watch.parse_time(item.get("fetched_at"))
        rows, seen = item.get("history"), set()
        watch.require(isinstance(rows, list) and rows, "invalid existing daily observations")
        for row in rows:
            watch.require(isinstance(row, dict) and row.get("insCode") == code,
                          "existing daily record insCode mismatch")
            day = trends.session_date(row.get("dEven"))
            watch.require(day not in seen and trends.number(row.get("pClosing")) is not None,
                          "duplicate date or invalid close in existing history")
            seen.add(day)
    return ids


@dataclass(frozen=True)
class Observation:
    code: str
    symbols: tuple
    raw: bytes
    fetched_at: str
    capture_id: str
    capture_body_sha256: str
    local_capture_proof: tuple = ()

    @property
    def key(self):
        return self.symbols[0] if len(self.symbols) == 1 else f"insCode:{self.code}"

    def entry(self):
        watch.require(re.fullmatch(r"(manual|legacy):[1-9][0-9]{0,7}", self.capture_id)
                      and re.fullmatch(r"[a-f0-9]{64}", self.capture_body_sha256), "missing capture provenance")
        rows = validate_history(self.raw, self.code, self.fetched_at)
        return {
            "code": self.code, "fetched_at": self.fetched_at, "record_count": len(rows),
            "quality": {"valid_close_count": len(rows),
                        "active_session_count": sum(row["_quality"]["volume_valid"] for row in rows),
                        "valid_ohlc_count": sum(row["_quality"]["ohlc_valid"] for row in rows)},
            "history": rows,
            "provenance": {
                "source_url": f"{BASE_URL}/{self.code}/{RECORD_LIMIT}", "http_status": 200,
                "body_sha256": watch.digest(self.raw), "byte_count": len(self.raw),
                "capture_id": self.capture_id, "capture_body_sha256": self.capture_body_sha256,
                "adjustment_status": "UNKNOWN", "units": "UNKNOWN", "session_completeness": "UNKNOWN",
                "historical_publication_time": "UNKNOWN", "instrument_type": "UNKNOWN",
                "point_in_time_verified": False,
                **({"http_status": None, "source_evidence": "OPERATOR_SUPPLIED_RECEIPT_AND_URL",
                    **dict(self.local_capture_proof)} if self.local_capture_proof else {}),
            },
        }


@dataclass(frozen=True)
class CSVObservation:
    code: str
    symbols: tuple
    raw: bytes
    source_sha256: str
    instrument_page_url: str
    fetched_at: str | None
    capture: dict
    close_policy: str = "strict"

    @property
    def key(self):
        return self.symbols[0]

    def entry(self):
        rows, ticker = validate_csv_history(self.raw, self.source_sha256, self.code, received_at=self.fetched_at,
                                            close_policy=self.close_policy)
        active = sum(row["_quality"]["volume_valid"] for row in rows)
        flagged = [{
            "session_date": trends.session_date(row["dEven"]).isoformat(),
            "reason": row["_quality"]["close_anomaly"],
            "source_fields": row["_source_fields"],
        } for row in rows if "close_anomaly" in row["_quality"]]
        # Keep complete source cells for audit, but never pass a flagged row to the ordinary analyzer.
        history = [row for row in rows if "close_anomaly" not in row["_quality"]]
        entry = {
            "code": self.code, "fetched_at": self.fetched_at, "record_count": len(rows),
            "quality": {"valid_close_count": len(history), "active_session_count": active,
                        "valid_ohlc_count": active - len(flagged), "zero_volume_count": len(rows) - active,
                        "zero_volume_unusual_price_dates": [
                            row["dEven"] for row in rows if row["_quality"]["zero_volume_unusual_prices"]
                        ]},
            "history": history,
            "provenance": {
                "source_format": trends.CSV_SOURCE_FORMAT, "source_url": None, "http_status": None,
                "instrument_page_url": self.instrument_page_url, "ticker": ticker,
                "source_evidence": "OPERATOR_SUPPLIED_CSV_AND_INSTRUMENT_PAGE",
                "body_sha256": self.source_sha256, "byte_count": len(self.raw),
                "identity_verification": "operator_attributed_with_archival_corroboration",
                "ins_code_present_in_source": False,
                "receipt_status": "UNKNOWN" if self.fetched_at is None else "OPERATOR_SUPPLIED",
                "capture_id": self.capture["capture_id"], "capture_body_sha256": self.capture["body_sha256"],
                "capture_receipt_tehran": self.capture["receipt_tehran"],
                **{key: self.capture[key] for key in ("seal_sha256", "manifest_sha256") if self.capture.get(key)},
                "adjustment_status": "UNKNOWN", "units": "UNKNOWN", "session_completeness": "UNKNOWN",
                "historical_publication_time": "UNKNOWN", "instrument_type": "UNKNOWN",
                "point_in_time_verified": False,
            },
        }
        if self.close_policy != "strict":
            entry["close_filter"] = {
                "series_basis": "conservative_filtered_sensitivity",
                "flagged_row_count": len(flagged), "flagged_rows": flagged,
                "close_above_high_count": sum(row["reason"] == "close_above_traded_high" for row in flagged),
                "close_below_low_count": sum(row["reason"] == "close_below_traded_low" for row in flagged),
            }
        return entry


@dataclass(frozen=True)
class MergePlan:
    baseline: bytes
    replacement: bytes

    def verify_source(self, path):
        """Validate the snapshot for offline merge preparation; not an atomic CAS."""
        _, current = watch.read_object(path, watch.MAX_BODY)
        watch.require(current == self.baseline, "history source changed since preparation")
        return current  # exact bytes for the separately approved backup


def member_spans(text, start):
    """Locate values in an already validated JSON object without reformatting it."""
    decoder = json.JSONDecoder()
    skip = re.compile(r"[ \t\r\n]*").match
    spans = {}
    pos = skip(text, start + 1).end()
    while text[pos] != "}":
        key, end = decoder.raw_decode(text, pos)
        start_value = skip(text, skip(text, end).end() + 1).end()
        _, pos = decoder.raw_decode(text, start_value)
        spans[key] = (start_value, pos)
        pos = skip(text, pos).end()
        if text[pos] == ",":
            pos = skip(text, pos + 1).end()
    return spans, pos


def name_key(name):
    return unicodedata.normalize("NFKC", name).strip().casefold().translate(str.maketrans("يك", "یک"))


def prepare_merge(baseline, observations, *, preview_unknown_csv_code=None):
    document = decode_object(baseline, watch.MAX_BODY)
    ids = history_ids(document)
    symbols = document["symbols"]
    names = {name_key(name) for name in symbols}
    additions = {}
    for observation in observations:
        watch.require(watch.positive_id(observation.code) and observation.code not in ids,
                      "existing or duplicate stable instrument ID")
        key = observation.key
        watch.require(name_key(key) and name_key(key) not in names, "ambiguous display-name key")
        additions[key] = observation.entry()
        ids.add(observation.code)
        names.add(name_key(key))
    if not additions:
        return MergePlan(baseline, baseline)
    count = str(len(symbols) + len(additions))
    entries = json.dumps(additions, ensure_ascii=False, allow_nan=False, indent=2)[1:-1]
    text = baseline.decode("utf-8")
    top, _ = member_spans(text, len(text) - len(text.lstrip(" \t\r\n")))
    metadata, meta_end = member_spans(text, top["metadata"][0])
    if "symbol_count" in metadata:
        lo, hi = metadata["symbol_count"]
        edits = [(lo, hi, count)]
    else:
        edits = [(meta_end, meta_end, ("," if metadata else "") + '"symbol_count":' + count)]
    end = top["symbols"][1] - 1
    edits.append((end, end, ("," if symbols else "") + entries))
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    replacement = text.encode("utf-8")
    watch.require(len(replacement) <= watch.MAX_BODY, "merged history exceeds reader limit")
    merged = decode_object(replacement, watch.MAX_BODY)
    watch.require(history_ids(merged, preview_unknown_csv_code=preview_unknown_csv_code) == ids and merged == (document | {
        "symbols": symbols | additions, "metadata": document["metadata"] | {"symbol_count": int(count)},
    }), "merged history did not validate")
    return MergePlan(baseline, replacement)


@dataclass(frozen=True)
class PinnedPath:
    path: Path
    parent_fd: int

    @property
    def name(self):
        return self.path.name

    @property
    def location(self):
        info = os.fstat(self.parent_fd)
        return info.st_dev, info.st_ino, self.name

    def read(self, limit):
        fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.parent_fd)
        with os.fdopen(fd, "rb") as file:
            before = os.fstat(file.fileno())
            watch.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                          f"not a regular, non-aliased file: {self.path}")
            watch.require(before.st_size <= limit, f"file exceeds size limit: {self.path}")
            raw = file.read(limit + 1)
            after = os.fstat(file.fileno())
            named = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
            watch.require(len(raw) == before.st_size and len(raw) <= limit
                          and file_version(before) == file_version(after) == file_version(named),
                          f"file changed while reading: {self.path}")
        return raw, file_version(after)

    def require_absent(self):
        try:
            os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ValueError(f"backup already exists: {self.path}")


def file_version(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def pinned_path(path):
    path = Path(path).absolute()
    watch.require(path.anchor == "/" and path.name and ".." not in path.parts, f"ambiguous path: {path}")
    # Android permits traversal but not directory reads on ancestors such as /.
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        parent = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            yield PinnedPath(path, parent)
        finally:
            os.close(parent)
    finally:
        os.close(fd)


def is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def capture_symbols(capture, bases, code, *, csv_source=False):
    watch.require(code in bases, "instrument absent from selected capture")
    watch.require(not any(row["excluded_from_options"] for row in capture["diagnostics"]),
                  "archive identity diagnostics must be resolved before import or overlay")
    symbols = tuple(sorted(bases[code]))
    if csv_source:
        watch.require(len(symbols) == 1 and name_key(symbols[0]) and not any(
            name_key(symbols[0]) == name_key(other) for other_code, names in bases.items()
            if other_code != code for other in names
        ), "ambiguous display name in selected capture")
    return symbols


def csv_research_observation(*, source, code, source_sha256, instrument_page_url, capture, bases,
                             close_policy="strict"):
    """Load a manual export for a caller's already verified archive capture; never merge it."""
    watch.require(watch.positive_id(code), "invalid stable instrument ID")
    watch.require(is_sha256(source_sha256), "invalid expected provider SHA-256")
    watch.require(instrument_page_url == f"{INSTRUMENT_PAGE}{code}", "unexpected source URL or instrument ID")
    watch.require(close_policy in CSV_CLOSE_POLICIES, "invalid CSV close policy")
    symbols = capture_symbols(capture, bases, code, csv_source=True)
    with pinned_path(source) as file:
        raw, _ = file.read(MAX_CSV_BYTES)
    return CSVObservation(code, symbols, raw, source_sha256, instrument_page_url, None, capture, close_policy)


def coverage_result(history):
    return {
        "status": "AVAILABLE" if history["status"] == "RESEARCH_ONLY" else "NOT_AVAILABLE",
        "reason_code": history["reason_code"],
        "valid_closes_before_cutoff": (history["history"] or {}).get("valid_close_count"),
    }


def import_coverage(document, bases, receipt):
    index = {item["code"]: [(name, item)] for name, item in document["symbols"].items()}
    indexed = None, document["metadata"], index, None
    return {code: coverage_result(combined.indexed_history(indexed, code, receipt)) for code in bases}


def csv_import_preview(document, observation, receipt):
    item = document["symbols"][observation.key]
    metadata = document["metadata"]
    indexed = None, metadata, {observation.code: [(observation.key, item)]}, None
    normal = combined.indexed_history(indexed, observation.code, receipt)
    if not trends.unknown_csv_receipt(item):
        return normal, normal
    report = trends.research_report(observation.key, item, metadata, before=receipt.date(),
                                    expected_code=observation.code, preview_unknown_csv_receipt=True)
    computed = any(metric["status"] == "COMPUTED" for metric in report["indicators"].values())
    proposal = {
        "history": report, "status": "RESEARCH_ONLY" if computed else "UNKNOWN",
        "reason_code": "csv_capture_bound_preview_only" if computed else "insufficient_history",
    }
    return proposal, normal


def local_import(*, source, source_url, source_sha256, received_at, code, capture_id, history, backup,
                 source_format="json", apply=False, expected_history_sha256=None, stream=collector.STREAM,
                 legacy_archive=watch.DEFAULT_ARCHIVE):
    """Validate and preview local material; publication is not implemented here."""
    watch.require(apply is False, PUBLICATION_DISABLED)
    watch.require(watch.positive_id(code), "invalid stable instrument ID")
    watch.require(is_sha256(source_sha256), "invalid expected provider SHA-256")
    watch.require(source_format in ("json", "tsetmc-csv"), "unexpected history source format")
    is_csv = source_format == "tsetmc-csv"
    expected_url = f"{INSTRUMENT_PAGE}{code}" if is_csv else f"{BASE_URL}/{code}/{RECORD_LIMIT}"
    watch.require(source_url == expected_url, "unexpected source URL or instrument ID")
    if not (is_csv and received_at is None):
        watch.require(isinstance(received_at, str) and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
            r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
            received_at) and not received_at.endswith("-00:00"), "receipt timestamp must include a known UTC offset")
        watch.require(watch.parse_time(received_at) <= datetime.now(timezone.utc), "receipt timestamp is in the future")
    watch.require(expected_history_sha256 is None or is_sha256(expected_history_sha256),
                  "invalid expected destination SHA-256")
    capture, bases = capture_inventory(capture_id, stream=stream, legacy_archive=legacy_archive)
    symbols = capture_symbols(capture, bases, code, csv_source=is_csv)
    with ExitStack() as stack:
        src, dest, save = [stack.enter_context(pinned_path(path)) for path in (source, history, backup)]
        watch.require(len({item.location for item in (src, dest, save)}) == 3,
                      "source, destination and backup must be distinct")
        protected = (Path(stream).resolve(), Path(legacy_archive).resolve(),
                     (watch.ROOT / "data/observations").resolve())
        watch.require(not any(item.path.is_relative_to(root) for item in (dest, save) for root in protected),
                      "destination and backup must be outside protected archives")
        save.require_absent()
        raw, _ = src.read(MAX_CSV_BYTES if is_csv else MAX_RESPONSE)
        watch.require(watch.digest(raw) == source_sha256, "provider payload SHA-256 mismatch")
        baseline, _ = dest.read(watch.MAX_BODY)
        digest = watch.digest(baseline)
        watch.require(expected_history_sha256 is None or expected_history_sha256 == digest,
                      "destination SHA-256 differs from expected history")
        proof = tuple((key, capture[key]) for key in ("seal_sha256", "manifest_sha256") if key in capture)
        observation = (CSVObservation(code, symbols, raw, source_sha256, source_url, received_at, capture) if is_csv
                       else Observation(code, symbols, raw, received_at, capture_id, capture["body_sha256"], proof))
        plan = prepare_merge(baseline, (observation,),
                             preview_unknown_csv_code=code if is_csv and received_at is None else None)
        old, new = (decode_object(content, watch.MAX_BODY) for content in (baseline, plan.replacement))
        receipt = watch.parse_time(capture["receipt_tehran"])
        before = import_coverage(old, bases, receipt)
        if is_csv:
            proposal, normal = csv_import_preview(new, observation, receipt)
            after = coverage_result(proposal)
        else:
            after = import_coverage(new, {code: bases[code]}, receipt)[code]
        available_before = sum(row["status"] == "AVAILABLE" for row in before.values())
        result = {
            "mode": "dry-run", "source": str(src.path),
            "destination": str(dest.path), "backup_required": str(save.path),
            "expected_history_sha256": digest, "proposed_history_sha256": watch.digest(plan.replacement),
            "instrument_id": code, "symbols": sorted(bases[code]), "capture_id": capture_id,
            "provenance": new["symbols"][observation.key]["provenance"], "received_at": received_at,
            "capture_coverage": {"total": len(bases), "before_available": available_before,
                                 "after_available": available_before + (after["status"] == "AVAILABLE"),
                                 "instrument_before": before[code], "instrument_after": after},
            "point_in_time_verified": False, "execution_eligible": False,
        }
        if is_csv:
            report = proposal["history"] or {}
            item = new["symbols"][observation.key]
            missing = sum(row["reason_code"] == "history_id_not_found" for row in before.values())
            result["capture_coverage"].update({
                "before_missing_ids": missing, "after_missing_ids": missing - 1,
                "preview_only_without_download_receipt": received_at is None,
                "standard_research_reader": coverage_result(normal),
            })
            result["csv_validation"] = item["quality"] | {
                "record_count": item["record_count"],
                "latest_session": trends.session_date(item["history"][0]["dEven"]).isoformat(),
                "earliest_session": trends.session_date(item["history"][-1]["dEven"]).isoformat(),
                **{key: report[key] for key in ("session_date_cutoff_exclusive", "records_before_cutoff",
                                                "excluded_at_or_after_cutoff", "valid_close_date_range")
                   if key in report},
            }
        return result


class StopCollection(ValueError):
    """Do not continue the batch after access/protocol errors."""


def request_history(opener, code, timeout):
    watch.require(watch.positive_id(code), "invalid stable instrument ID")
    url = f"{BASE_URL}/{code}/{RECORD_LIMIT}"
    request = Request(url, headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                    "User-Agent": "TSETMC-Underlying-History/manual"})
    try:
        opened = opener.open(request, timeout=timeout)
    except HTTPError as error:
        error.close()
        if error.code in (404, 410):
            raise ValueError(f"HTTP {error.code}: history unavailable") from error
        raise StopCollection(f"HTTP {error.code}: collection stopped") from error
    with opened as response:
        if response.status != 200 or response.geturl() != url:
            raise StopCollection("unexpected HTTP status or redirect")
        headers = response.headers
        media = headers.get_all("Content-Type", [])
        coding = headers.get_all("Content-Encoding", [])
        if (len(media) != 1 or media[0].split(";", 1)[0].strip().lower() != "application/json"
                or [v.lower() for v in coding] not in ([], ["identity"])):
            raise StopCollection("unexpected HTTP content type or encoding")
        lengths = headers.get_all("Content-Length", [])
        transfer = headers.get_all("Transfer-Encoding", [])
        if lengths:
            watch.require(len(lengths) == 1 and not transfer and re.fullmatch(r"[0-9]{1,9}", lengths[0]),
                          "invalid or ambiguous HTTP framing")
            watch.require(int(lengths[0]) <= MAX_RESPONSE, "HTTP response too large")
        else:
            watch.require([v.lower() for v in transfer] in ([], ["chunked"]), "invalid HTTP framing")
        raw = response.read(MAX_RESPONSE + 1)
        fetched_at = datetime.now(timezone.utc).isoformat()
        watch.require(len(raw) <= MAX_RESPONSE and (not lengths or len(raw) == int(lengths[0])),
                      "HTTP response too large or length mismatch")
    return raw, fetched_at


def collect_missing(capture_id, codes, *, max_requests, delay=MIN_DELAY, timeout=REQUEST_TIMEOUT,
                    stream=collector.STREAM, legacy_archive=watch.DEFAULT_ARCHIVE, history_path=OUTPUT_FILE):
    """Explicit library entry point for a later authorized run; no disk writes."""
    codes = tuple(islice(codes, MAX_REQUESTS + 1))
    watch.require(type(max_requests) is int and 1 <= len(codes) <= max_requests <= MAX_REQUESTS,
                  "invalid request budget")
    watch.require(all(watch.positive_id(code) for code in codes) and len(set(codes)) == len(codes),
                  "explicit unique stable IDs required")
    watch.require(type(delay) in (int, float) and math.isfinite(delay) and delay >= MIN_DELAY
                  and type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= REQUEST_TIMEOUT,
                  "invalid pacing or timeout")
    report = preview(capture_id, stream=stream, legacy_archive=legacy_archive, history_path=history_path)
    watch.require(report["history_sha256"] is not None, "a readable existing history baseline is required")
    document, baseline = watch.read_object(history_path, watch.MAX_BODY, report["history_sha256"])
    existing = history_ids(document)
    watch.require(not any(row["excluded_from_options"] for row in report["capture"]["diagnostics"]),
                  "archive identity diagnostics must be resolved before collection")
    watch.require(all(code not in existing and code in report["missing_ids"] for code in codes),
                  "only missing IDs from the selected capture may be requested")
    observations, failures = [], {}
    source = MergePlan(baseline, baseline)
    names_by_id = {code: tuple(report["instruments"][code]["symbols"]) for code in codes}
    keys = [names[0] if len(names) == 1 else f"insCode:{code}" for code, names in names_by_id.items()]
    watch.require(len(set(keys)) == len(keys) and not set(keys) & document["symbols"].keys(),
                  "ambiguous display-name key")
    opener = build_opener(ProxyHandler({}), collector.NoRedirect())
    stopped = False
    for index, code in enumerate(codes):
        if index:
            time.sleep(delay)
        source.verify_source(history_path)
        try:
            raw, fetched = request_history(opener, code, timeout)
            observation = Observation(code, names_by_id[code], raw, fetched,
                                      capture_id, report["capture"]["body_sha256"])
            observation.entry()
        except (StopCollection, OSError, HTTPException) as error:
            failures[code] = str(error)
            stopped = True
            break
        except ValueError as error:
            failures[code] = str(error)
        else:
            observations.append(observation)
    source.verify_source(history_path)
    return {"merge": prepare_merge(baseline, observations), "observations": tuple(observations),
            "failures": failures, "stopped": stopped}


def main(argv=None):
    parser = argparse.ArgumentParser(description="پیش‌نمایش و ورود محلی تاریخچهٔ پایه؛ بدون شبکه")
    parser.add_argument("mode", nargs="?", choices=("preview", "collect", "import"))
    parser.add_argument("--capture", help="شناسهٔ دقیق manual:N یا legacy:N")
    parser.add_argument("--history", type=Path)
    parser.add_argument("--stream", type=Path, default=collector.STREAM)
    parser.add_argument("--legacy-archive", type=Path, default=watch.DEFAULT_ARCHIVE)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--source-format", choices=("json", "tsetmc-csv"))
    parser.add_argument("--source-url", help="نشانی پاسخ JSON یا صفحهٔ اصلی ابزار برای CSV")
    parser.add_argument("--source-sha256")
    parser.add_argument("--received-at")
    parser.add_argument("--ins-code")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true", help="غیرفعال؛ فقط پیش‌نمایش مجاز است")
    parser.add_argument("--expected-history-sha256")
    args = parser.parse_args(argv)
    if args.apply:
        print(PUBLICATION_DISABLED, file=sys.stderr)
        return 2
    import_fields = (args.source, args.source_url, args.source_sha256, args.received_at, args.ins_code, args.backup)
    if args.mode != "import" and (any(value is not None for value in (*import_fields, args.source_format))
                                  or args.expected_history_sha256 is not None):
        parser.error("import options require import mode")
    if args.mode == "collect":
        print("برداشت زنده در CLI غیرفعال است؛ مجوز جداگانه لازم است.", file=sys.stderr)
        return 2
    if args.mode is None:
        parser.print_help()
        return 0 if args.capture is None else 2
    if args.capture is None:
        parser.error("preview and import require --capture")
    try:
        if args.mode == "import":
            if (any(value is None for value in (args.source, args.source_url, args.source_sha256,
                                                args.ins_code, args.history, args.backup))
                    or (args.source_format != "tsetmc-csv" and args.received_at is None)):
                parser.error("import requires --source, --source-url, --source-sha256, --ins-code, --history "
                             "and --backup; --received-at may be omitted only for CSV")
            result = local_import(source=args.source, source_url=args.source_url, source_sha256=args.source_sha256,
                                  received_at=args.received_at, code=args.ins_code, capture_id=args.capture,
                                  history=args.history, backup=args.backup,
                                   expected_history_sha256=args.expected_history_sha256,
                                   source_format=args.source_format or "json",
                                  stream=args.stream, legacy_archive=args.legacy_archive)
        else:
            result = preview(args.capture, stream=args.stream, legacy_archive=args.legacy_archive,
                             history_path=args.history or OUTPUT_FILE)
    except (OSError, ValueError, ZoneInfoNotFoundError) as error:
        print(f"عملیات تاریخچه ناموفق بود: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
