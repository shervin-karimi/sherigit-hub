"""Validation boundary for local, operator-controlled provider data. No I/O to providers."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


TEHRAN = ZoneInfo("Asia/Tehran")
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


def trading_date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("trading date must be YYYY-MM-DD")
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("trading date must be YYYY-MM-DD")
    return day


@dataclass(frozen=True)
class Instrument:
    ins_code: str
    symbol: str
    option_type: str
    contract_size: int
    source: str
    expires_at: datetime
    first_trading_date: date | None = None
    last_trading_date: date | None = None

    def pre_last_trade_deadline(self):
        if self.last_trading_date is None:
            return None
        day = self.last_trading_date - timedelta(days=10)
        return timestamp(datetime.combine(day, time.min, TEHRAN))

    def trade_cutoff(self):
        if self.last_trading_date is None:
            return None
        if self.last_trading_date == self.expires_at.astimezone(TEHRAN).date():
            return self.expires_at
        next_day = self.last_trading_date + timedelta(days=1)
        return timestamp(datetime.combine(next_day, time.min, TEHRAN))

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict):
            raise ValueError("instrument must be a JSON object")
        kind = data.get("option_type")
        if kind not in ("CALL", "PUT"):
            raise ValueError("option_type must be CALL or PUT")
        expiry = timestamp(data.get("expires_at"))
        first = trading_date(data.get("first_trading_date"))
        last = trading_date(data.get("last_trading_date"))
        if last is not None and (last <= date.min + timedelta(days=10)
                                 or last > expiry.astimezone(TEHRAN).date()
                                 or (first is not None and first > last)):
            raise ValueError("invalid contract trading date range")
        return cls(
            text(data.get("ins_code"), "ins_code"), text(data.get("symbol"), "symbol"), kind,
            positive_int(data.get("contract_size"), "contract_size"),
            text(data.get("source"), "provider source"), expiry, first, last,
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
        cutoff = instrument.trade_cutoff()
        if cutoff is not None and now >= cutoff:
            raise ValueError("final trading date has ended; official settlement evidence is required")
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
