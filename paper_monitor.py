"""Foreground monitor for local provider snapshots. Runs no broker or network code."""

import fcntl
import json
import signal
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from time import monotonic, sleep

from paper_quotes import Quote, positive_int, timestamp
from paper_trading import incident, resolve


def read_feed(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def select_quotes(records, now, catalog):
    """Pick the newest validated quote per instrument, rejecting conflicting ties."""
    newest, conflicts = {}, set()
    for record in records or ():
        try:
            quote = Quote.parse(record, now, catalog)
        except ValueError:
            continue
        code = quote.instrument.ins_code
        previous = newest.get(code)
        if previous is None or quote.quoted_at > previous.quoted_at:
            newest[code] = quote
            conflicts.discard(code)
        elif quote.quoted_at == previous.quoted_at:
            if (quote.bid, quote.ask) != (previous.bid, previous.ask):
                conflicts.add(code)
            elif quote.received_at < previous.received_at:
                newest[code] = quote
    return {code: quote for code, quote in newest.items() if code not in conflicts}


def heartbeat(db, now, interval, state, data_state, *, success=False):
    db.execute(
        """INSERT INTO paper_monitor VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET state = excluded.state, heartbeat_at = excluded.heartbeat_at,
        last_success_at = COALESCE(excluded.last_success_at, paper_monitor.last_success_at),
        interval_seconds = excluded.interval_seconds, data_state = excluded.data_state""",
        (state, now.isoformat(), now.isoformat() if success else None, interval, data_state),
    )


def heartbeat_lost(row, now):
    age = now - timestamp(row["heartbeat_at"])
    return row["state"] != "stopped" and (
        age < timedelta() or age > timedelta(seconds=2 * row["interval_seconds"] + 5)
    )


class PaperMonitor:
    def __init__(self, ledger, interval=15):
        self.ledger = ledger
        self.interval = positive_int(interval, "poll interval seconds", 3600)

    def cycle(self, records):
        ledger = self.ledger
        ledger.require_policy()
        with ledger.transaction() as db:
            now = timestamp(ledger.clock())
            ledger._account(db)
            previous = db.execute("SELECT * FROM paper_monitor WHERE id = 1").fetchone()
            if previous is not None and heartbeat_lost(previous, now):
                incident(db, "heartbeat_gap", "critical",
                         f"monitor heartbeat interrupted; previous: {previous['heartbeat_at']}", now)
                resolve(db, "heartbeat_gap", now)
            quotes = select_quotes(records, now, ledger.catalog)
            state = "available" if quotes else "unavailable" if records is None else "no_valid_quotes"
            if quotes:
                resolve(db, "feed", now)
            else:
                incident(db, "feed", "warning", state, now)
            for row in ledger._open(db):
                if not ledger.owns(row):
                    continue
                pid = row["id"]
                blocked = ledger.observe_position(db, row, now)
                if blocked in ("expired", "unverified"):
                    resolve(db, f"quote:{pid}", now)
                    if ledger.due(row, now):
                        incident(db, f"failed_exit:{pid}", "critical", blocked, now)
                    continue
                quote = quotes.get(row["ins_code"])
                if quote is None:
                    incident(db, f"quote:{pid}", "warning", "quote missing, stale or unsafe", now)
                    if blocked == "overdue":
                        incident(db, f"failed_exit:{pid}", "critical", "no executable exit quote", now)
                    continue
                sale = (f"monitor:{pid}", row["remaining"], "required_exit") if blocked == "overdue" else None
                try:
                    ledger.apply_quote(db, row, quote, now, sale=sale)
                    resolve(db, f"processing:{pid}", now)
                except ValueError as error:
                    incident(db, f"processing:{pid}", "critical", str(error), now)
                    if sale is not None:
                        incident(db, f"failed_exit:{pid}", "critical", str(error), now)
            resolve(db, "monitor_error", now)
            heartbeat(db, now, self.interval, "running", state, success=True)

    def failure(self):
        with self.ledger.transaction() as db:
            now = timestamp(self.ledger.clock())
            incident(db, "monitor_error", "critical", "monitor cycle rolled back", now)
            heartbeat(db, now, self.interval, "error", "unknown")

    def stop(self):
        with self.ledger.transaction() as db:
            now = timestamp(self.ledger.clock())
            previous = db.execute("SELECT data_state FROM paper_monitor WHERE id = 1").fetchone()
            heartbeat(db, now, self.interval, "stopped", previous[0] if previous else "unknown")

    def status(self):
        with self.ledger.transaction(write=False) as db:
            self.ledger._account(db)
            row = db.execute("SELECT * FROM paper_monitor WHERE id = 1").fetchone()
            if row is None:
                return {"state": "never_started", "effective_state": "never_started"}
            now = timestamp(self.ledger.clock())
            age = now - timestamp(row["heartbeat_at"])
            state = "unresponsive" if heartbeat_lost(row, now) else row["state"]
            return {**dict(row), "effective_state": state, "heartbeat_age_seconds": age.total_seconds()}

    def incidents(self):
        with self.ledger.transaction(write=False) as db:
            return [dict(row) for row in db.execute("SELECT * FROM paper_incidents ORDER BY id")]

    @contextmanager
    def _lock(self):
        path = self.ledger.path.resolve()
        if not path.is_file():
            raise ValueError("paper account is not initialized")
        with path.with_name(path.name + ".monitor.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("a monitor is already running for this paper ledger") from error
            yield

    def run(self, feed, *, once=False):
        stopped = False

        def request_stop(_signum, _frame):
            nonlocal stopped
            stopped = True

        with self._lock():
            signals = (signal.SIGINT, signal.SIGTERM)
            previous = {sig: signal.signal(sig, request_stop) for sig in signals}
            try:
                while not stopped:
                    try:
                        self.cycle(read_feed(feed))
                    except (sqlite3.Error, ValueError):
                        self.failure()
                        if once:
                            raise
                    if once:
                        break
                    wake_at = monotonic() + self.interval
                    while not stopped and (remaining := wake_at - monotonic()) > 0:
                        sleep(min(remaining, 1))
            finally:
                try:
                    self.stop()
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
