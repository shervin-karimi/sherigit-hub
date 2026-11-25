"""Local-only, long-option paper ledger. Prices and cash are in whole rials."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from paper_limits import BotPolicy
from paper_quotes import MAX_QUOTE_AGE, MAX_SQLITE_INT, TEHRAN, Instrument, Quote, positive_int, text, timestamp


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


OWNERSHIP_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS paper_ownership (
        position_id INTEGER PRIMARY KEY REFERENCES positions(id),
        bot_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
        remaining INTEGER NOT NULL CHECK (remaining >= 0)
    )""",
    """CREATE TABLE IF NOT EXISTS paper_exits (
        order_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
        position_id INTEGER NOT NULL REFERENCES paper_ownership(position_id),
        bot_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        price INTEGER NOT NULL CHECK (price > 0), filled_at TEXT NOT NULL,
        quote TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS paper_exits_position ON paper_exits(position_id)""",
)

POSITION_ROWS = """SELECT p.*, o.bot_id, o.strategy_id, o.remaining
    FROM positions p LEFT JOIN paper_ownership o ON o.position_id = p.id"""


def order_key(value):
    value = text(value, "order_id")
    if len(value) > 128 or value.startswith("monitor:"):
        raise ValueError("invalid or reserved order_id")
    return value


def request_hash(*values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


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
    def __init__(self, path=DB_PATH, clock=None, *, catalog=None, sessions=None, policy=None):
        if policy is not None and not isinstance(policy, BotPolicy):
            raise ValueError("invalid bot policy")
        self.policy = policy
        self.path = Path(path)
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.catalog = {} if catalog is None else catalog
        self.sessions = {} if sessions is None else sessions

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
            if (not self._has_table(db, "paper_state") or not self._has_trade_cutoff(db)
                    or not self._has_table(db, "paper_exits")):
                if not write:
                    db.rollback()
                    db.execute("BEGIN IMMEDIATE")
                if not self._has_table(db, "account") or not self._has_table(db, "positions"):
                    raise ValueError("paper account is not initialized")
                if not self._has_table(db, "paper_state"):
                    for statement in UPGRADE:
                        db.execute(statement)
                if not self._has_trade_cutoff(db):
                    db.execute("ALTER TABLE positions ADD COLUMN trade_cutoff_at TEXT")
                for statement in OWNERSHIP_SCHEMA:
                    db.execute(statement)
            yield db

    @staticmethod
    def _has_trade_cutoff(db):
        return any(column["name"] == "trade_cutoff_at" for column in db.execute("PRAGMA table_info(positions)"))

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
        policy = self.require_policy()
        order_id = order_key(order_id)
        digest = request_hash(data, quantity)
        with self.transaction() as db:
            now = timestamp(self.clock())
            account = self._account(db)
            existing = db.execute(POSITION_ROWS + " WHERE p.order_id = ?", (order_id,)).fetchone()
            if existing is not None:
                if not self.owns(existing) or existing["request_hash"] != digest:
                    raise ValueError("order_id already used with a different request")
                return self._view(db, existing, now, account["stop_loss_bps"])
            if db.execute("SELECT 1 FROM paper_exits WHERE order_id = ?", (order_id,)).fetchone():
                raise ValueError("order_id already used for a sell")
            open_trades = self._open(db)
            blocked = [
                self.observe_position(db, row, now) if self.owns(row)
                else bool(self.due(row, now) or self.settlement_required(row, now, self.trade_cutoff(row))
                          or self.matching_instrument(row) is None or self.trade_cutoff(row) is None)
                for row in open_trades
            ]
            if not any(blocked) and not self._paused(db):
                quote = Quote.parse(data, now, self.catalog)
                return self._buy(db, quote, quantity, order_id, digest, now, account, open_trades, policy)
        # Commit recorded incidents even when the entry is rejected.
        if any(blocked):
            raise ValueError("exit overdue or resolve expired/unverified paper positions before buying")
        raise ValueError("paper entries are paused")

    def _buy(self, db, quote, quantity, order_id, digest, now, account, open_trades, policy):
        instrument = quote.instrument
        cost = quote.ask * instrument.contract_size * quantity
        for row in open_trades:
            if row["bot_id"] is not None:
                self.verify_inventory(db, row)
        allocated = sum(self._cost(row) for row in open_trades)
        if allocated + cost > account["initial_equity"] // 10:
            raise ValueError("total open purchase cost would exceed 10% of initial equity")
        if cost > account["cash"]:
            raise ValueError("insufficient paper cash")
        limits = policy.limits
        purchased, loss = self.daily_usage(db, now)
        if loss is None or loss >= limits.max_daily_loss:
            raise ValueError("daily loss limit reached or loss evidence unavailable")
        for actual, limit, label in (
            (quantity, limits.max_buy_quantity, "buy quantity"),
            (cost, limits.max_buy_value, "buy order value"),
            (quote.ask, limits.max_buy_price, "buy limit price"),
            (purchased + cost, limits.max_daily_purchase_value, "daily purchase value"),
            (allocated + cost, limits.max_open_exposure, "total open exposure"),
        ):
            if actual > limit:
                raise ValueError(f"{label} limit exceeded")
        deadline = instrument.pre_last_trade_deadline()
        first = instrument.first_trading_date
        if first is None or deadline is None or first.isoformat() not in self.sessions:
            raise ValueError("verified first/last trading dates and session calendar are required")
        if now >= deadline:
            raise ValueError("paper entry cutoff has been reached")
        local_quote = quote.quoted_at.astimezone(TEHRAN)
        completed = sum(
            first <= (day := date.fromisoformat(key)) <= local_quote.date()
            and local_quote >= datetime.combine(day, spans[-1][1], TEHRAN)
            for key, spans in self.sessions.items()
        )
        if completed < 5:
            raise ValueError("five trading sessions must be completed before entry")
        at = quote.quoted_at.isoformat()
        audit = json.dumps(quote.audit(now))
        cur = db.execute(
            """INSERT INTO positions (
                ins_code, symbol, option_type, quantity, contract_size, entry_price,
                entry_quote_at, opened_at, exit_due_at, last_bid, last_quote_at,
                order_id, request_hash, entry_quote, last_quote, expires_at, trade_cutoff_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instrument.ins_code, instrument.symbol, instrument.option_type, quantity,
             instrument.contract_size, quote.ask, at, now.isoformat(),
             min(now + MAX_HOLD, deadline).isoformat(), quote.bid, at, order_id, digest,
             audit, audit, instrument.expires_at.isoformat(), instrument.trade_cutoff().isoformat()),
        )
        db.execute("UPDATE account SET cash = cash - ? WHERE id = 1", (cost,))
        db.execute("INSERT INTO paper_ownership VALUES (?, ?, ?, ?)",
                   (cur.lastrowid, policy.bot_id, policy.strategy_id, quantity))
        row = self.position(db, cur.lastrowid)
        return self._view(db, row, now, account["stop_loss_bps"])

    def require_policy(self):
        if self.policy is None:
            raise ValueError("verified bot identity and paper limits are required")
        return self.policy

    def owns(self, row):
        return self.policy is not None and row is not None and (
            row["bot_id"], row["strategy_id"]
        ) == (self.policy.bot_id, self.policy.strategy_id)

    @staticmethod
    def position(db, position_id):
        return db.execute(POSITION_ROWS + " WHERE p.id = ?", (position_id,)).fetchone()

    def mark(self, position_id, data):
        """Update an owned bid mark without submitting an exit."""
        return self._update(position_id, data)

    def sell(self, position_id, data, *, order_id, quantity=None):
        order_id = order_key(order_id)
        if quantity is not None:
            positive_int(quantity, "sell quantity")
        return self._update(position_id, data, sale=(order_id, quantity))

    def _update(self, position_id, data, *, sale=None):
        positive_int(position_id, "position id")
        self.require_policy()
        with self.transaction() as db:
            now = timestamp(self.clock())
            account = self._account(db)
            row = self.position(db, position_id)
            if not self.owns(row):
                raise ValueError("position ownership cannot be verified")
            if sale is not None:
                order_id, quantity = sale
                digest = request_hash(position_id, data, quantity)
                previous = db.execute("SELECT * FROM paper_exits WHERE order_id = ?", (order_id,)).fetchone()
                if previous is not None:
                    if not self.owns(previous) or previous["request_hash"] != digest:
                        raise ValueError("order_id already used with a different request or owner")
                    return self._view(db, row, now, account["stop_loss_bps"])
                if db.execute("SELECT 1 FROM positions WHERE order_id = ?", (order_id,)).fetchone():
                    raise ValueError("order_id already used for a buy")
                sale = (order_id, quantity if quantity is not None else row["remaining"], digest)
            if row["closed_at"] is not None:
                raise ValueError("open paper position not found")
            blocked = self.observe_position(db, row, now)
            if blocked == "expired":
                error = ValueError("trading has ended; official settlement evidence is required")
            elif blocked == "unverified":
                error = ValueError("verified contract metadata or final trading date is unavailable")
            else:
                try:
                    quote = Quote.parse(data, now, self.catalog)
                    self.apply_quote(db, row, quote, now, sale=sale)
                except ValueError as failure:
                    error = failure
                else:
                    return self._view(db, self.position(db, position_id), now, account["stop_loss_bps"])
            if sale is not None and self.due(row, now):
                incident(db, f"failed_exit:{position_id}", "critical", str(error), now)
        raise error

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

    def matching_instrument(self, row):
        instrument = self.catalog.get(row["ins_code"])
        if instrument is not None and instrument.matches(row) and (
            not row["expires_at"] or instrument.expires_at == timestamp(row["expires_at"])
        ) and (
            not row["entry_quote"] or json.loads(row["entry_quote"])["source"] == instrument.source
        ):
            return instrument
        return None

    def exit_deadline(self, row):
        stored = timestamp(row["exit_due_at"])
        instrument = self.matching_instrument(row)
        deadline = instrument.pre_last_trade_deadline() if instrument else None
        return min(stored, deadline) if deadline is not None else stored

    def trade_cutoff(self, row):
        instrument = self.matching_instrument(row)
        candidates = (
            timestamp(row["trade_cutoff_at"]) if row["trade_cutoff_at"] else None,
            instrument.trade_cutoff() if instrument else None,
        )
        return min((cutoff for cutoff in candidates if cutoff is not None), default=None)

    @staticmethod
    def settlement_required(row, now, cutoff):
        return bool(row["expiry_seen_at"]) or (
            cutoff is not None and now >= cutoff
        ) or (
            row["expires_at"] is not None and now >= timestamp(row["expires_at"])
        )

    def due(self, row, now):
        return row["exit_pending_at"] is not None or now >= self.exit_deadline(row)

    def observe_position(self, db, row, now):
        """Persist exit requests and return why a position blocks entries, if any."""
        if not self.owns(row):
            raise ValueError("position ownership cannot be verified")
        pid = row["id"]
        deadline = self.exit_deadline(row)
        if deadline < timestamp(row["exit_due_at"]):
            db.execute("UPDATE positions SET exit_due_at = ? WHERE id = ?", (deadline.isoformat(), pid))
        due = row["exit_pending_at"] is not None or now >= deadline
        if due:
            db.execute(
                "UPDATE positions SET exit_pending_at = ? WHERE id = ? AND exit_pending_at IS NULL",
                (now.isoformat(), pid),
            )
            incident(db, f"holding:{pid}", "critical", "holding deadline reached; exit requested", now)
        instrument = self.matching_instrument(row)
        cutoff = self.trade_cutoff(row)
        if cutoff is not None and cutoff.isoformat() != row["trade_cutoff_at"]:
            db.execute("UPDATE positions SET trade_cutoff_at = ? WHERE id = ?", (cutoff.isoformat(), pid))
        if not row["expires_at"] and instrument is not None:
            db.execute("UPDATE positions SET expires_at = ? WHERE id = ?", (instrument.expires_at.isoformat(), pid))
        if self.settlement_required(row, now, cutoff):
            db.execute(
                "UPDATE positions SET expiry_seen_at = ? WHERE id = ? AND expiry_seen_at IS NULL",
                (now.isoformat(), pid),
            )
            incident(db, f"expiry:{pid}", "critical", "trading ended; official settlement evidence required", now)
            return "expired"
        if instrument is None or cutoff is None:
            incident(db, f"metadata:{pid}", "critical", "verified contract metadata or final trading date unavailable", now)
            return "unverified"
        resolve(db, f"metadata:{pid}", now)
        return "overdue" if due else None

    def check_position_quote(self, row, quote, now):
        cutoff = self.trade_cutoff(row)
        if self.settlement_required(row, now, cutoff):
            raise ValueError("trading has ended; official settlement evidence is required")
        if cutoff is None:
            raise ValueError("verified final trading date is unavailable")
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

    def apply_quote(self, db, row, quote, now, *, sale=None):
        """Apply a validated provider quote inside the caller's write transaction."""
        if not self.owns(row):
            raise ValueError("position ownership cannot be verified")
        self.verify_inventory(db, row)
        self.check_position_quote(row, quote, now)
        pid = row["id"]
        if sale is not None:
            self._fill_exit(db, row, quote, now, *sale)
        at = quote.quoted_at.isoformat()
        if sale is not None or row["last_quote"] is None or at != row["last_quote_at"]:
            db.execute(
                "UPDATE positions SET last_bid = ?, last_quote_at = ?, last_quote = ? WHERE id = ?",
                (quote.bid, at, json.dumps(quote.audit(now)), pid),
            )
        stop = self._account(db)["stop_loss_bps"]
        remaining = row["remaining"] - (sale[1] if sale else 0)
        if remaining and (row["entry_price"] - quote.bid) * 10000 >= row["entry_price"] * stop:
            incident(db, f"stop_loss:{pid}", "warning", "paper stop-loss threshold reached", now)
        else:
            resolve(db, f"stop_loss:{pid}", now)
        resolve(db, f"quote:{pid}", now)

    def _fill_exit(self, db, row, quote, now, order_id, quantity, digest):
        policy = self.require_policy()
        limits = policy.limits
        if db.execute("SELECT 1 FROM positions WHERE order_id = ?", (order_id,)).fetchone():
            raise ValueError("order_id already used for a buy")
        if not 0 < quantity <= row["remaining"]:
            raise ValueError("sell quantity exceeds available bot-owned quantity")
        proceeds = quote.bid * quantity * row["contract_size"]
        if quantity > limits.max_sell_quantity or proceeds > limits.max_sell_value:
            raise ValueError("sell quantity or order value limit exceeded")
        if quote.bid < limits.min_sell_price:
            raise ValueError("minimum sell price limit breached")
        self.check_execution_clock(db, now)
        cash = self._account(db)["cash"] + proceeds
        if cash > MAX_SQLITE_INT:
            raise ValueError("simulated cash would exceed the ledger's integer range")
        pid = row["id"]
        remaining = row["remaining"] - quantity
        db.execute("INSERT INTO paper_exits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            order_id, digest, pid, policy.bot_id, policy.strategy_id, quantity,
            quote.bid, now.isoformat(), json.dumps(quote.audit(now)),
        ))
        db.execute("UPDATE paper_ownership SET remaining = ? WHERE position_id = ?", (remaining, pid))
        db.execute("UPDATE account SET cash = ? WHERE id = 1", (cash,))
        if remaining == 0:
            late = max(0, (now - self.exit_deadline(row)) // timedelta(microseconds=1))
            db.execute(
                """UPDATE positions SET exit_price = ?, closed_at = ?, exit_reason = ?,
                exit_late_us = ? WHERE id = ?""",
                (quote.bid, now.isoformat(), "holding_limit" if self.due(row, now) else "manual", late, pid),
            )
            resolve(db, f"holding:{pid}", now)
            resolve(db, f"failed_exit:{pid}", now)

    def verify_inventory(self, db, row):
        fills = db.execute(
            "SELECT * FROM paper_exits WHERE position_id = ?", (row["id"],)
        ).fetchall()
        if (not row["bot_id"] or not row["strategy_id"] or not row["order_id"]
                or not row["request_hash"] or not row["entry_quote"]
                or row["remaining"] != row["quantity"] - sum(fill["quantity"] for fill in fills)
                or any(self.fill_pnl(row, fill) is None for fill in fills)):
            raise ValueError("bot-owned inventory evidence is inconsistent")

    @staticmethod
    def check_execution_clock(db, now):
        times = db.execute("""
            SELECT opened_at AS occurred_at FROM positions
            UNION ALL SELECT closed_at FROM positions WHERE closed_at IS NOT NULL
            UNION ALL SELECT filled_at FROM paper_exits
        """)
        if any(timestamp(row["occurred_at"]) > now for row in times):
            raise ValueError("clock is older than recorded paper activity")

    def daily_usage(self, db, now):
        day = now.astimezone(TEHRAN).date()
        purchased = 0
        loss = 0
        for row in db.execute(POSITION_ROWS):
            opened = timestamp(row["opened_at"])
            if opened > now:
                raise ValueError("clock is older than recorded paper activity")
            if opened.astimezone(TEHRAN).date() == day:
                purchased += row["entry_price"] * row["quantity"] * row["contract_size"]
            if row["bot_id"] is None and row["closed_at"]:
                closed = timestamp(row["closed_at"])
                if closed > now:
                    raise ValueError("clock is older than recorded paper activity")
                if closed.astimezone(TEHRAN).date() == day:
                    pnl = self.realized_pnl(row)
                    loss = None if pnl is None or loss is None else loss + max(0, -pnl)
        for fill in db.execute("SELECT * FROM paper_exits"):
            filled = timestamp(fill["filled_at"])
            if filled > now:
                raise ValueError("clock is older than recorded paper activity")
            if filled.astimezone(TEHRAN).date() == day:
                pnl = self.fill_pnl(self.position(db, fill["position_id"]), fill)
                loss = None if pnl is None or loss is None else loss + max(0, -pnl)
        return purchased, loss

    def status(self):
        with self.transaction(write=False) as db:
            now = timestamp(self.clock())
            account = self._account(db)
            positions = [self._view(db, row, now, account["stop_loss_bps"]) for row in self._open(db)]
            allocated = sum(pos["purchase_cost"] for pos in positions)
            marks = [pos["unrealized_pnl"] for pos in positions]
            rows = db.execute(POSITION_ROWS + " WHERE p.closed_at IS NOT NULL OR o.remaining < p.quantity")
            realized = [self.position_pnl(db, row) for row in rows]
            report = None
            if self.policy is not None:
                purchased, loss = self.daily_usage(db, now)
                report = self.policy.report(purchased=purchased, loss=loss)
            return {
                **dict(account),
                "entries_paused": self._paused(db),
                "allocated": allocated,
                "allocation_limit": account["initial_equity"] // 10,
                "realized_pnl": None if None in realized else sum(realized),
                "unrealized_pnl": None if None in marks else sum(marks),
                "positions": positions,
                "گزارش حدود و ظرفیت روزانه (ریال)": report,
            }

    def history(self):
        with self.transaction(write=False) as db:
            now = timestamp(self.clock())
            stop = self._account(db)["stop_loss_bps"]
            return [self._view(db, row, now, stop) for row in db.execute(POSITION_ROWS + " ORDER BY p.id")]

    @staticmethod
    def _account(db):
        row = db.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise ValueError("paper account is not initialized")
        return row

    @staticmethod
    def _open(db):
        return db.execute(POSITION_ROWS + " WHERE p.closed_at IS NULL ORDER BY p.id").fetchall()

    @staticmethod
    def _cost(row):
        quantity = row["quantity"] if row["remaining"] is None else row["remaining"]
        return row["entry_price"] * quantity * row["contract_size"]

    def fill_pnl(self, row, fill):
        if row is None or (fill["bot_id"], fill["strategy_id"]) != (row["bot_id"], row["strategy_id"]):
            return None
        fill_row = dict(row)
        fill_row.update(closed_at=fill["filled_at"], exit_price=fill["price"],
                        last_quote=fill["quote"], quantity=fill["quantity"])
        try:
            fill_row["last_quote_at"] = json.loads(fill["quote"])["quoted_at"]
        except (KeyError, TypeError, ValueError):
            return None
        return self.realized_pnl(fill_row)

    def position_pnl(self, db, row):
        if row["bot_id"] is None:
            return self.realized_pnl(row)
        fills = db.execute("SELECT * FROM paper_exits WHERE position_id = ? ORDER BY rowid", (row["id"],)).fetchall()
        if not fills or sum(fill["quantity"] for fill in fills) != row["quantity"] - row["remaining"]:
            return None
        if row["closed_at"] and (row["exit_price"], row["closed_at"], row["last_quote"]) != (
            fills[-1]["price"], fills[-1]["filled_at"], fills[-1]["quote"]
        ):
            return None
        values = [self.fill_pnl(row, fill) for fill in fills]
        return None if None in values else sum(values)

    def realized_pnl(self, row):
        """Report a historical fill only when its execution evidence can be checked."""
        cutoff = self.trade_cutoff(row)
        if not row["closed_at"] or cutoff is None or not row["last_quote"] or not row["entry_quote"]:
            return None
        try:
            closed = timestamp(row["closed_at"])
            audit = json.loads(row["last_quote"])
            source = json.loads(row["entry_quote"])["source"]
            if not timestamp(row["opened_at"]) <= closed < cutoff or timestamp(audit["processed_at"]) != closed:
                return None
            expires = timestamp(row["expires_at"])
            code = row["ins_code"]
            instrument = Instrument(code, row["symbol"], row["option_type"], row["contract_size"], source, expires)
            quote = Quote.parse({
                "ins_code": code, "symbol": row["symbol"], "option_type": row["option_type"],
                "contract_size": row["contract_size"], "source": audit["source"],
                "quote_class": audit["quote_class"], "best_bid": audit["bid"], "best_ask": audit["ask"],
                "quoted_at": audit["quoted_at"], "received_at": audit["received_at"],
            }, closed, {code: instrument})
            if (quote.bid != row["exit_price"] or quote.quoted_at != timestamp(row["last_quote_at"])
                    or quote.quoted_at < timestamp(row["entry_quote_at"])):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return (quote.bid - row["entry_price"]) * row["quantity"] * row["contract_size"]

    def _view(self, db, row, now, stop_bps):
        view = dict(row)
        view.pop("request_hash")
        for field in ("entry_quote", "last_quote"):
            view[field] = json.loads(row[field]) if row[field] else None
        remaining = row["quantity"] if row["remaining"] is None else row["remaining"]
        units = remaining * row["contract_size"]
        closed = row["closed_at"] is not None
        overdue = not closed and self.due(row, now)
        if not closed:
            view["exit_due_at"] = self.exit_deadline(row).isoformat()
        expired = not closed and self.settlement_required(row, now, self.trade_cutoff(row))
        fresh = not closed and not expired and view["last_quote"] and (
            timestamp(view["last_quote"]["processed_at"]) <= now
            and timedelta(0) <= now - timestamp(row["last_quote_at"]) <= MAX_QUOTE_AGE
        )
        pnl = (row["last_bid"] - row["entry_price"]) * units if fresh else None
        cost = self._cost(row)
        view.update(
            status=("CLOSED" if closed else "EXPIRY_REVIEW" if expired
                    else "EXIT_PENDING" if overdue else "OPEN"),
            purchase_cost=cost,
            unrealized_pnl=pnl,
            realized_pnl=self.position_pnl(db, row),
            exit_orders=[{key: fill[key] for key in ("order_id", "quantity", "price", "filled_at")}
                         for fill in db.execute("SELECT * FROM paper_exits WHERE position_id = ? ORDER BY rowid",
                                                (row["id"],))],
            stop_loss_alert=None if pnl is None else -pnl * 10000 >= cost * stop_bps,
        )
        return view
