"""Trust boundary for separately supplied paper-only quote and entry batches."""

import hashlib
import hmac
import json
import os
import stat

from paper_quotes import MAX_QUOTE_AGE, Quote, positive_int, text, timestamp
from shadow_collector import json_bytes
from shadow_watch import invalid_constant, unique_object


MAX_FEED = 2 * 1024 * 1024


def private_bytes(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise ValueError(f"required private file is missing: {path.name}") from error
    with os.fdopen(fd, "rb") as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
            raise ValueError(f"file must be private and owned by the pilot user: {path.name}")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"file exceeds size limit: {path.name}")
    return raw


def private_json(path, limit=MAX_FEED):
    raw = private_bytes(path, limit)
    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"invalid JSON in {path.name}") from error


def signed_batch(feed, key_path, now, catalog):
    key = private_bytes(key_path, 64)
    if len(key) < 32:
        raise ValueError("trusted producer HMAC key must contain 32–64 raw bytes")
    envelope = private_json(feed)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "hmac_sha256"}:
        raise ValueError("missing authenticated quote envelope")
    payload, signature = envelope["payload"], envelope["hmac_sha256"]
    try:
        canonical = json_bytes(payload)
    except (ValueError, RecursionError) as error:
        raise ValueError("invalid signed payload") from error
    expected = hmac.digest(key, canonical, "sha256").hex()
    if (not isinstance(signature, str) or not signature.isascii()
            or not hmac.compare_digest(expected, signature)):
        raise ValueError("trusted producer signature does not match")
    fields = {"schema", "sequence", "issued_at", "evidence_ref", "quotes", "entries"}
    if (not isinstance(payload, dict) or set(payload) != fields
            or type(payload["schema"]) is not int or payload["schema"] != 1):
        raise ValueError("unsupported trusted quote batch schema")
    positive_int(payload["sequence"], "signed batch sequence")
    reference = text(payload["evidence_ref"], "independent quote/contract evidence reference")
    if len(reference) > 256 or not reference.isprintable():
        raise ValueError("invalid evidence reference")
    issued = timestamp(payload["issued_at"])
    if issued > now or now - issued > MAX_QUOTE_AGE:
        raise ValueError("trusted batch issuance is future-dated or stale")
    if not isinstance(payload["quotes"], list) or not isinstance(payload["entries"], list):
        raise ValueError("trusted quotes and entry instructions must be lists")
    quotes = {}
    for data in payload["quotes"]:
        quote = Quote.parse(data, now, catalog)
        if quote.received_at > issued:
            raise ValueError("quote receipt is later than signed batch issuance")
        code = quote.instrument.ins_code
        if code in quotes:
            raise ValueError("duplicate instrument in signed quote batch")
        quotes[code] = data
    orders = set()
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"ins_code", "market", "order_id", "quantity"}:
            raise ValueError("invalid signed entry instruction")
        code = text(entry["ins_code"], "signed entry instrument")
        order = text(entry["order_id"], "signed entry order ID")
        positive_int(entry["quantity"], "signed entry quantity")
        if code not in quotes or order in orders:
            raise ValueError("signed entry has no matching quote or repeats an order ID")
        orders.add(order)
    return payload, quotes, hashlib.sha256(canonical).hexdigest(), issued
