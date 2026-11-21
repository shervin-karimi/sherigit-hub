"""Local-only, long-option paper ledger. Prices and cash are in whole rials."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_quotes import MAX_QUOTE_AGE, MAX_SQLITE_INT, Quote, positive_int, text, timestamp


DB_PATH = Path(__file__).resolve().parent / "data" / "paper_trading.db"
MAX_HOLD = timedelta(days=7)


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        initial_equity INTEGER NOT NULL CHECK (initial_equity > 0),
        cash INTEGER NOT NULL CHECK (cash >= 0),
        stop_loss_bps INTEGER NOT NULL CHECK (stop_loss_bps BETWEEN 1 AND 10000)
    )""",
    """CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY,
        ins_code TEXT NOT NULL,
        symbol TEXT NOT NULL,
        option_type TEXT NOT NULL CHECK (option_type IN ('CALL', 'PUT')),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        contract_size INTEGER NOT NULL CHECK (contract_size > 0),
        entry_price INTEGER NOT NULL,
        entry_quote_at TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        exit_due_at TEXT NOT NULL,
        last_bid INTEGER NOT NULL,
        last_quote_at TEXT NOT NULL,
        exit_price INTEGER,
        closed_at TEXT,
        exit_reason TEXT
    )""",
)

UPGRADE = (
    "ALTER TABLE positions ADD COLUMN order_id TEXT",
    "ALTER TABLE positions ADD COLUMN request_hash TEXT",
    "ALTER TABLE positions ADD COLUMN entry_quote TEXT",
    "ALTER TABLE positions ADD COLUMN last_quote TEXT",
    "ALTER TABLE positions ADD COLUMN expires_at TEXT",
    "ALTER TABLE positions ADD COLUMN expiry_seen_at TEXT",
    "ALTER TABLE positions ADD COLUMN exit_pending_at TEXT",
    "ALTER TABLE positions ADD COLUMN exit_late_us INTEGER",
    "CREATE UNIQUE INDEX paper_order_id ON positions(order_id)",
    """CREATE TABLE paper_incidents (
        id INTEGER PRIMARY KEY, incident_key TEXT NOT NULL, severity TEXT NOT NULL,
        opened_at TEXT NOT NULL, detail TEXT NOT NULL, resolved_at TEXT
    )""",
    """CREATE UNIQUE INDEX paper_active_incident ON paper_incidents(incident_key)
        WHERE resolved_at IS NULL""",
    """CREATE TABLE paper_monitor (
        id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL, last_success_at TEXT, interval_seconds INTEGER NOT NULL,
        data_state TEXT NOT NULL
    )""",
    """CREATE TABLE paper_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), entries_paused INTEGER NOT NULL
            CHECK (entries_paused IN (0, 1))
    )""",
    "INSERT INTO paper_state VALUES (1, 0)",
)


def incident(db, key, severity, detail, now):
    db.execute(
        """INSERT OR IGNORE INTO paper_incidents (incident_key, severity, opened_at, detail)
        VALUES (?, ?, ?, ?)""", (key, severity, now.isoformat(), detail),
    )


def resolve(db, key, now):
    db.execute(
        "UPDATE paper_incidents SET resolved_at = ? WHERE incident_key = ? AND resolved_at IS NULL",
        (now.isoformat(), key),
    )


class PaperTrading:
    def __init__(self, path=DB_PATH, clock=None, *, catalog=None):
        self.path = Path(path)
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.catalog = {} if catalog is None else catalog

    @staticmethod
    def _has_table(db, name):
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    @contextmanager
    def transaction(self, *, write=True, creating=False):
        if not creating and not self.path.is_file():
            raise ValueError("paper account is not initialized")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if creating and self._has_table(db, "trades"):
                raise ValueError("choose a separate database for paper trading")
            if creating:
                for statement in SCHEMA:
                    db.execute(statement)
            if not self._has_table(db, "paper_state"):
                if not write:
                    db.rollback()
                    db.execute("BEGIN IMMEDIATE")
                if not self._has_table(db, "account") or not self._has_table(db, "positions"):
                    raise ValueError("paper account is not initialized")
                if not self._has_table(db, "paper_state"):
                    for statement in UPGRADE:
                        db.execute(statement)
            yield db

    def initialize(self, initial_equity, stop_loss_bps=300):
        positive_int(initial_equity, "initial equity")
        positive_int(stop_loss_bps, "stop loss in basis points", 10000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction(creating=True) as db:
            if db.execute("SELECT 1 FROM account WHERE id = 1").fetchone():
                raise ValueError("paper account is already initialized")
            db.execute(
                "INSERT INTO account VALUES (1, ?, ?, ?)",
                (initial_equity, initial_equity, stop_loss_bps),
            )

    def buy(self, data, quantity, *, order_id):
        positive_int(quantity, "quantity")
        order_id = text(order_id, "order_id")
        if len(order_id) > 128:
            raise ValueError("order_id must be at most 128 characters")
        digest = hashlib.sha256(json.dumps([data, quantity], sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            now = timestamp(self.clock())
            account = self._account(db)
            existing = db.execute("SELECT * FROM positions WHERE order_id = ?", (order_id,)).fetchone()
            if existing is not None:
                if existing["request_hash"] != digest:
                    raise ValueError("order_id already used with a different request")
                return self._view(existing, now, account["stop_loss_bps"])
            open_trades = self._open(db)
            blocked = [self.observe_position(db, row, now) for row in open_trades]
            if not any(blocked) and not self._paused(db):
                quote = Quote.parse(data, now, self.catalog)
                return self._buy(db, quote, quantity, order_id, digest, now, account, open_trades)
        # Commit recorded incidents even when the entry is rejected.
        if any(blocked):
            raise ValueError("exit overdue or resolve expired/unverified paper positions before buying")
        raise ValueError("paper entries are paused")

    def _buy(self, db, quote, quantity, order_id, digest, now, account, open_trades):
        instrument = quote.instrument
        cost = quote.ask * instrument.contract_size * quantity
        allocated = sum(self._cost(row) for row in open_trades)
        if allocated + cost > account["initial_equity"] // 10:
            raise ValueError("total open purchase cost would exceed 10% of initial equity")
        if cost > account["cash"]:
            raise ValueError("insufficient paper cash")
        at = quote.quoted_at.isoformat()
        audit = json.dumps(quote.audit(now))
        cur = db.execute(
            """INSERT INTO positions (
                ins_code, symbol, option_type, quantity, contract_size, entry_price,
                entry_quote_at, opened_at, exit_due_at, last_bid, last_quote_at,
                order_id, request_hash, entry_quote, last_quote, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instrument.ins_code, instrument.symbol, instrument.option_type, quantity,
             instrument.contract_size, quote.ask, at, now.isoformat(),
             (now + MAX_HOLD).isoformat(), quote.bid, at, order_id, digest,
             audit, audit, instrument.expires_at.isoformat()),
        )
        db.execute("UPDATE account SET cash = cash - ? WHERE id = 1", (cost,))
        row = db.execute("SELECT * FROM positions WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._view(row, now, account["stop_loss_bps"])

    def mark(self, position_id, data):
        """Update the bid mark without submitting an exit."""
        return self._update(position_id, data, sell=False)

    def sell(self, position_id, data):
        return self._update(position_id, data, sell=True)

    def _update(self, position_id, data, *, sell):
        positive_int(position_id, "position id")
        with self.transaction() as db:
            now = timestamp(self.clock())
            account = self._account(db)
            row = db.execute(
                "SELECT * FROM positions WHERE id = ? AND closed_at IS NULL", (position_id,)
            ).fetchone()
            if row is None:
                raise ValueError("open paper position not found")
            quote = Quote.parse(data, now, self.catalog)
            self.check_position_quote(row, quote, now)
            self.observe_position(db, row, now)
            self.apply_quote(db, row, quote, now, sell=sell)
            row = db.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
            return self._view(row, now, account["stop_loss_bps"])

    @staticmethod
    def _paused(db):
        return bool(db.execute("SELECT entries_paused FROM paper_state WHERE id = 1").fetchone()[0])

    def set_pause(self, paused):
        with self.transaction() as db:
            self._account(db)
            db.execute("UPDATE paper_state SET entries_paused = ? WHERE id = 1", (int(paused),))
            now = timestamp(self.clock())
            if paused:
                incident(db, "entries_paused", "critical", "operator paused new entries", now)
            else:
                resolve(db, "entries_paused", now)

    @staticmethod
    def due(row, now):
        return row["exit_pending_at"] is not None or now >= timestamp(row["exit_due_at"])

    def observe_position(self, db, row, now):
        """Persist exit requests and return why a position blocks entries, if any."""
        pid = row["id"]
        due = self.due(row, now)
        if due:
            db.execute(
                "UPDATE positions SET exit_pending_at = ? WHERE id = ? AND exit_pending_at IS NULL",
                (now.isoformat(), pid),
            )
            incident(db, f"holding:{pid}", "critical", "holding deadline reached; exit requested", now)
        instrument = self.catalog.get(row["ins_code"])
        expiry = timestamp(row["expires_at"]) if row["expires_at"] else None
        if expiry is None and instrument and instrument.matches(row):
            expiry = instrument.expires_at
            db.execute("UPDATE positions SET expires_at = ? WHERE id = ?", (expiry.isoformat(), pid))
        if row["expiry_seen_at"] or (expiry is not None and now >= expiry):
            db.execute(
                "UPDATE positions SET expiry_seen_at = ? WHERE id = ? AND expiry_seen_at IS NULL",
                (now.isoformat(), pid),
            )
            incident(db, f"expiry:{pid}", "critical", "contract expired; verified settlement required", now)
            return "expired"
        if expiry is None or instrument is None or not instrument.matches(row) or (
            instrument.expires_at != expiry or (
                row["entry_quote"] and json.loads(row["entry_quote"])["source"] != instrument.source
            )
        ):
            incident(db, f"metadata:{pid}", "critical", "verified contract metadata unavailable or changed", now)
            return "unverified"
        resolve(db, f"metadata:{pid}", now)
        return "overdue" if due else None

    @staticmethod
    def check_position_quote(row, quote, now):
        if row["expiry_seen_at"]:
            raise ValueError("contract has expired; settlement requires verified rules")
        if not quote.instrument.matches(row) or (
            row["expires_at"] and quote.instrument.expires_at != timestamp(row["expires_at"])
        ):
            raise ValueError("quote does not match the position's contract")
        previous = json.loads(row["last_quote"]) if row["last_quote"] else None
        if now < timestamp(row["opened_at"]) or (
            row["exit_pending_at"] and now < timestamp(row["exit_pending_at"])
        ) or (
            previous and (quote.quoted_at < timestamp(row["last_quote_at"])
                          or now < timestamp(previous["processed_at"]))
        ):
            raise ValueError("clock or quote is older than the position's recorded data")
        if previous and (
            previous["source"] != quote.instrument.source or (
                quote.quoted_at == timestamp(row["last_quote_at"])
                and (quote.bid, quote.ask) != (previous["bid"], previous["ask"])
            )
        ):
            raise ValueError("source changed or conflicting prices at same quote timestamp")

    def apply_quote(self, db, row, quote, now, *, sell):
        """Apply a validated quote inside the caller's write transaction."""
        pid = row["id"]
        if sell:
            proceeds = quote.bid * row["quantity"] * row["contract_size"]
            cash = self._account(db)["cash"] + proceeds
            if cash > MAX_SQLITE_INT:
                raise ValueError("simulated cash would exceed the ledger's integer range")
            overdue = self.due(row, now)
            late = max(0, (now - timestamp(row["exit_due_at"])) // timedelta(microseconds=1))
            db.execute(
                """UPDATE positions SET exit_price = ?, closed_at = ?, exit_reason = ?,
                exit_late_us = ? WHERE id = ?""",
                (quote.bid, now.isoformat(), "holding_limit" if overdue else "manual", late, pid),
            )
            db.execute("UPDATE account SET cash = ? WHERE id = 1", (cash,))
            resolve(db, f"holding:{pid}", now)
        at = quote.quoted_at.isoformat()
        if sell or row["last_quote"] is None or at != row["last_quote_at"]:
            db.execute(
                "UPDATE positions SET last_bid = ?, last_quote_at = ?, last_quote = ? WHERE id = ?",
                (quote.bid, at, json.dumps(quote.audit(now)), pid),
            )
        resolve(db, f"quote:{pid}", now)

    def status(self):
        with self.transaction(write=False) as db:
            now = timestamp(self.clock())
            account = self._account(db)
            positions = [self._view(row, now, account["stop_loss_bps"]) for row in self._open(db)]
            allocated = sum(pos["purchase_cost"] for pos in positions)
            marks = [pos["unrealized_pnl"] for pos in positions]
            return {
                **dict(account),
                "entries_paused": self._paused(db),
                "allocated": allocated,
                "allocation_limit": account["initial_equity"] // 10,
                "realized_pnl": account["cash"] + allocated - account["initial_equity"],
                "unrealized_pnl": None if None in marks else sum(marks),
                "positions": positions,
            }

    def history(self):
        with self.transaction(write=False) as db:
            now = timestamp(self.clock())
            stop = self._account(db)["stop_loss_bps"]
            return [self._view(row, now, stop) for row in db.execute(
                "SELECT * FROM positions ORDER BY id"
            )]

    @staticmethod
    def _account(db):
        row = db.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise ValueError("paper account is not initialized")
        return row

    @staticmethod
    def _open(db):
        return db.execute("SELECT * FROM positions WHERE closed_at IS NULL ORDER BY id").fetchall()

    @staticmethod
    def _cost(row):
        return row["entry_price"] * row["quantity"] * row["contract_size"]

    @classmethod
    def _view(cls, row, now, stop_bps):
        view = dict(row)
        view.pop("request_hash")
        for field in ("entry_quote", "last_quote"):
            view[field] = json.loads(row[field]) if row[field] else None
        units = row["quantity"] * row["contract_size"]
        closed = row["closed_at"] is not None
        overdue = not closed and cls.due(row, now)
        expired = not closed and (
            row["expiry_seen_at"] or (row["expires_at"] and now >= timestamp(row["expires_at"]))
        )
        fresh = not closed and not expired and view["last_quote"] and (
            timestamp(view["last_quote"]["processed_at"]) <= now
            and timedelta(0) <= now - timestamp(row["last_quote_at"]) <= MAX_QUOTE_AGE
        )
        pnl = (row["last_bid"] - row["entry_price"]) * units if fresh else None
        cost = cls._cost(row)
        view.update(
            status=("CLOSED" if closed else "EXPIRY_REVIEW" if expired
                    else "EXIT_PENDING" if overdue else "OPEN"),
            purchase_cost=cost,
            unrealized_pnl=pnl,
            realized_pnl=(row["exit_price"] - row["entry_price"]) * units if closed else None,
            stop_loss_alert=None if pnl is None else -pnl * 10000 >= cost * stop_bps,
        )
        return view
