"""One manual public TSETMC observation; no polling, orders or account access."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import shadow_watch as watch


STREAM = watch.ROOT / "data/observations/tsetmc-options/manual-v1"
SYNTHETIC = "synthetic_multiday_fixture"
TEHRAN = timezone(timedelta(hours=3, minutes=30))
MIN_INTERVAL = timedelta(seconds=60)
RECORD = re.compile(r"[0-9]{8}")
FILES = {"body.raw.json", "receipt.json", "seal.json"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def utc_now():
    return datetime.now(timezone.utc)


def fetch():
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(watch.SOURCE, headers={
        "Accept": "application/json", "Accept-Encoding": "identity",
        "Cache-Control": "no-cache", "User-Agent": "TSETMC-Shadow-Watch/manual",
    })
    with opener.open(request, timeout=15) as response:
        watch.require(response.status == 200 and response.geturl() == watch.SOURCE,
                      "unexpected HTTP response source or status")
        watch.require(response.headers.get("Content-Encoding", "identity").lower() == "identity",
                      "unexpected HTTP content encoding")
        raw = response.read(watch.MAX_BODY + 1)
        received = utc_now()
        headers = {key: response.headers[key] for key in
                   ("Date", "Content-Type", "ETag", "Age", "Last-Modified")
                   if key in response.headers}
    return raw, received, headers


@contextmanager
def locked(root, exclusive):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise watch.ArchiveError("observation archive is busy") from error
        yield fd
    finally:
        os.close(fd)


def utc(value):
    result = watch.parse_time(value)
    watch.require(result.utcoffset() == timedelta(0), "expected a UTC receipt or request")
    return result


def read_stream(root, dataset_class=watch.DATA_CLASS):
    """Read under a directory lock; verify chain, payloads, clock, and sequence."""
    watch.require(dataset_class in (watch.DATA_CLASS, SYNTHETIC), "unsupported dataset class")
    records, seen, previous = [], set(), None
    for seq, entry in enumerate(sorted(root.iterdir()), start=1):
        watch.require(RECORD.fullmatch(entry.name) and entry.name == f"{seq:08d}",
                      f"missing, unexpected or unfinished stream record: {entry.name}")
        watch.require(not entry.is_symlink() and entry.is_dir()
                      and {item.name for item in entry.iterdir()} == FILES,
                      f"missing or unsafe record files: {entry.name}")
        seal, seal_raw = watch.read_object(entry / "seal.json", watch.MAX_META)
        prior_hash = previous["head_sha256"] if previous else None
        watch.require(seal.get("previous_seal_sha256", "") == prior_hash
                      and type(seal.get("sequence")) is int and seal["sequence"] == seq
                      and isinstance(seal.get("receipt_sha256"), str),
                      f"invalid seal or broken hash chain: {entry.name}")
        meta, _ = watch.read_object(entry / "receipt.json", watch.MAX_META, seal["receipt_sha256"])
        watch.require(type(meta.get("schema_version")) is int and meta["schema_version"] == 1
                      and type(meta.get("sequence")) is int and meta["sequence"] == seq
                      and meta.get("dataset_class") == dataset_class
                      and meta.get("source_url") == (watch.SOURCE if dataset_class == watch.DATA_CLASS else None)
                      and meta.get("http_status") == 200 and meta.get("execution_eligible") is False
                      and all(key in meta and meta[key] is None for key in
                              ("exchange_quote_timestamp", "provider_quote_timestamp", "order_book_sequence"))
                      and isinstance(meta.get("body_sha256"), str), "unsupported receipt metadata")
        at, start = utc(meta.get("receipt_utc")), utc(meta.get("request_utc"))
        local = watch.parse_time(meta.get("receipt_tehran"))
        watch.require(start <= at and local == at and local.utcoffset() == TEHRAN.utcoffset(None)
                      and (not previous or start > utc(previous["receipt_utc"])),
                      "duplicate or out-of-order receipt/request clock")
        path = entry / "body.raw.json"
        _, raw = watch.read_object(path, watch.MAX_BODY, meta["body_sha256"])
        watch.require(len(raw) == meta.get("byte_count"), "raw body byte count mismatch")
        watch.require(meta["body_sha256"] not in seen, "duplicate body SHA-256")
        seen.add(meta["body_sha256"])
        previous = dict(meta, head_sha256=watch.digest(seal_raw), path=path)
        records.append(previous)
    return records


def sync_write(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fchmod(stream.fileno(), 0o400)
        os.fsync(stream.fileno())


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def append(root, fd, prior, raw, started, received, headers, *,
           dataset_class=watch.DATA_CLASS, blocked_hashes=()):
    """Write once while holding the exclusive lock; publish by directory rename."""
    watch.require(dataset_class in (watch.DATA_CLASS, SYNTHETIC), "unsupported dataset class")
    seq = len(prior) + 1
    watch.require(seq <= 99_999_999 and len(raw) <= watch.MAX_BODY, "stream or body limit exceeded")
    body_hash = watch.digest(raw)
    watch.require(body_hash not in blocked_hashes
                  and all(row["body_sha256"] != body_hash for row in prior), "duplicate body SHA-256")
    watch.require(started.utcoffset() == received.utcoffset() == timedelta(0) and started <= received
                  and (not prior or started > utc(prior[-1]["receipt_utc"])),
                  "duplicate or out-of-order receipt/request clock")
    target = root / f"{seq:08d}"
    watch.require(not os.path.lexists(target), "stream record already exists")
    with TemporaryDirectory(prefix=".pending-", dir=root) as staging:
        temp = Path(staging)
        body = temp / "body.raw.json"
        sync_write(body, raw)
        data, _ = watch.read_object(body, watch.MAX_BODY, body_hash)
        rows = data.get("instrumentOptMarketWatch")
        watch.require(isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows),
                      "empty or invalid TSETMC watchlist")
        meta = {
            "schema_version": 1, "sequence": seq, "dataset_class": dataset_class,
            "source_url": watch.SOURCE if dataset_class == watch.DATA_CLASS else None,
            "http_status": 200, "headers": headers, "byte_count": len(raw), "rows": len(rows),
            "body_sha256": body_hash, "request_utc": started.isoformat(),
            "receipt_utc": received.isoformat(), "receipt_tehran": received.astimezone(TEHRAN).isoformat(),
            "exchange_quote_timestamp": None, "provider_quote_timestamp": None,
            "order_book_sequence": None, "execution_eligible": False,
        }
        watch.contract_states(body, meta)  # Same schema and identity checks as offline replay.
        receipt_raw = json_bytes(meta)
        watch.require(len(receipt_raw) <= watch.MAX_META, "receipt limit exceeded")
        seal_raw = json_bytes({
            "sequence": seq, "receipt_sha256": watch.digest(receipt_raw),
            "previous_seal_sha256": prior[-1]["head_sha256"] if prior else None,
        })
        sync_write(temp / "receipt.json", receipt_raw)
        sync_write(temp / "seal.json", seal_raw)
        with locked(temp, exclusive=True) as temp_fd:
            os.fsync(temp_fd)
        temp.rename(target)
        os.fsync(fd)
    return dict(meta, head_sha256=watch.digest(seal_raw))


def collect():
    STREAM.mkdir(parents=True, exist_ok=True)
    with locked(STREAM, exclusive=True) as fd:
        prior = read_stream(STREAM)
        old = watch.records_in_order(watch.DEFAULT_ARCHIVE) if watch.DEFAULT_ARCHIVE.exists() else []
        started = utc_now()
        latest = utc(prior[-1]["receipt_utc"]) if prior else old[-1][0] if old else None
        watch.require(latest is None or started >= latest + MIN_INTERVAL,
                      "minimum 60 seconds between captures or local clock moved backward")
        raw, received, headers = fetch()
        return append(STREAM, fd, prior, raw, started, received, headers,
                      blocked_hashes={row[3]["body_sha256"] for row in old})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        result = collect()
    except (OSError, ValueError, HTTPException, subprocess.TimeoutExpired) as error:
        print(f"Capture stopped: {error}", file=sys.stderr)
        return 1
    print(json_bytes(result).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
