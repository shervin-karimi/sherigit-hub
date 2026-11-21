"""Validation boundary for local, operator-controlled provider data. No I/O to providers."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


MAX_QUOTE_AGE = timedelta(minutes=5)
MAX_SQLITE_INT = 2**63 - 1


def positive_int(value, name, maximum=MAX_SQLITE_INT):
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"{name} must be a positive integer (max {maximum})")
    return value


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timestamp must include a time zone")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError as error:
        raise ValueError("timestamp is out of range") from error


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


@dataclass(frozen=True)
class Instrument:
    ins_code: str
    symbol: str
    option_type: str
    contract_size: int
    source: str
    expires_at: datetime

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict):
            raise ValueError("instrument must be a JSON object")
        kind = data.get("option_type")
        if kind not in ("CALL", "PUT"):
            raise ValueError("option_type must be CALL or PUT")
        return cls(
            text(data.get("ins_code"), "ins_code"), text(data.get("symbol"), "symbol"), kind,
            positive_int(data.get("contract_size"), "contract_size"),
            text(data.get("source"), "provider source"), timestamp(data.get("expires_at")),
        )

    def matches(self, row):
        return all(getattr(self, field) == row[field]
                   for field in ("ins_code", "symbol", "option_type", "contract_size"))


def load_catalog(records):
    if not isinstance(records, list) or not records:
        raise ValueError("catalog must be a nonempty JSON array of verified contracts")
    catalog = {}
    for data in records:
        instrument = Instrument.parse(data)
        if instrument.ins_code in catalog:
            raise ValueError("duplicate ins_code in catalog")
        catalog[instrument.ins_code] = instrument
    return catalog


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    bid: int
    ask: int
    quoted_at: datetime
    received_at: datetime

    @classmethod
    def parse(cls, data, now, catalog):
        if not isinstance(data, dict):
            raise ValueError("quote must be a JSON object")
        code = text(data.get("ins_code"), "ins_code")
        kind = data.get("option_type")
        if kind not in ("CALL", "PUT"):
            raise ValueError("option_type must be CALL or PUT")
        size = positive_int(data.get("contract_size"), "contract_size")
        bid = positive_int(data.get("best_bid"), "best_bid")
        ask = positive_int(data.get("best_ask"), "best_ask")
        if bid > ask:
            raise ValueError("crossed quote: bid exceeds ask")
        at = timestamp(data.get("quoted_at"))
        received = timestamp(data.get("received_at"))
        if received > now or at > now:
            raise ValueError("quote or receipt timestamp is in the future")
        if at > received:
            raise ValueError("provider quote timestamp is later than local receipt")
        if now - at > MAX_QUOTE_AGE:
            raise ValueError("stale quote")
        if data.get("quote_class") != "provider":
            raise ValueError("manual or unverifiable quote cannot be used for execution or marks")
        instrument = catalog.get(code)
        if instrument is None or data.get("source") != instrument.source:
            raise ValueError("untrusted source or unknown instrument")
        if (kind, size, data.get("symbol")) != (
            instrument.option_type, instrument.contract_size, instrument.symbol
        ):
            raise ValueError("quote does not match verified contract identity or multiplier")
        if at >= instrument.expires_at or now >= instrument.expires_at:
            raise ValueError("contract has expired; settlement requires verified rules")
        return cls(instrument, bid, ask, at, received)

    def audit(self, now):
        return {
            "source": self.instrument.source,
            "quote_class": "provider",
            "quoted_at": self.quoted_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "processed_at": now.isoformat(),
            "bid": self.bid,
            "ask": self.ask,
        }
