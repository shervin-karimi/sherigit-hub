"""Local, append-only research decisions. This module has no ledger or order interface."""

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from paper_quotes import MAX_QUOTE_AGE, Quote, positive_int, timestamp
from rank_candidates import MAX_SPREAD, check_contract


SCOPE = "paper_research_only"
ALLOCATION_PERCENT = 10
HOLD_DAYS = 7
DECISIONS = {"APPROVED", "REJECTED", "CANCELLED"}
APP_ID = 0x54534541


def utc_now():
    return datetime.now(timezone.utc)


def iso(at):
    return timestamp(at).isoformat(timespec="microseconds")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RiskSnapshot:
    initial_equity_rials: int
    open_purchase_cost_rials: int
    cash_rials: int
    stop_loss_bps: int
    entries_allowed: bool  # Must reflect entry pause, overdue exits and unresolved positions.


@dataclass(frozen=True)
class Proposal:
    key: str
    payload: str
    valid_until: datetime

    @classmethod
    def from_verified_inputs(cls, ranked, quote, quantity, risk, evidence_ref, now):
        """For a trusted scanner adapter, NOT an importer for arbitrary local JSON.

        The adapter must independently verify the producer, exchange quote time,
        contract/cutoff, units and model evidence before calling this method.
        Phase 1 does not ship such an adapter.
        """
        now = timestamp(now)
        numeric_gates = ("remaining_days", "trade_count", "trade_volume", "bid_volume",
                         "ask_volume", "ask_time_value", "delta")
        if not isinstance(quote, Quote) or not isinstance(ranked, dict) or any(
            type(ranked.get(key)) not in (int, float) or not math.isfinite(ranked[key])
            for key in numeric_gates
        ):
            raise ValueError("verified quote and rank eligibility are required")
        instrument = quote.instrument
        # Repeat existing quote checks, including identity, age, source and cutoff.
        Quote.parse({
            **{key: getattr(instrument, key) for key in
               ("ins_code", "symbol", "option_type", "contract_size", "source")},
            "best_bid": quote.bid, "best_ask": quote.ask, "quote_class": "provider",
            "quoted_at": quote.quoted_at, "received_at": quote.received_at,
        }, now, {instrument.ins_code: instrument})
        if not instrument.matches(ranked) or (ranked["best_bid"], ranked["best_ask"]) != (quote.bid, quote.ask):
            raise ValueError("rank and quote details disagree")
        # Do not trust a precomputed spread or the rounding of a displayed value.
        spread = (quote.ask - quote.bid) * 200 / (quote.ask + quote.bid)
        if spread > MAX_SPREAD or not check_contract({**ranked, "spread_percent": spread})[0]:
            raise ValueError("rank eligibility is required")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip() or len(evidence_ref) > 256:
            raise ValueError("a bounded provenance reference is required")
        positive_int(quantity, "quantity")
        if quantity > positive_int(ranked.get("ask_volume"), "ask depth"):
            raise ValueError("quantity exceeds observed ask depth")
        cost = positive_int(quote.ask * instrument.contract_size * quantity, "maximum purchase cost")
        cap = positive_int(risk.initial_equity_rials, "initial equity") * ALLOCATION_PERCENT // 100
        if type(risk.open_purchase_cost_rials) is not int or not 0 <= risk.open_purchase_cost_rials <= cap:
            raise ValueError("invalid open purchase cost")
        positive_int(risk.cash_rials, "cash")
        positive_int(risk.stop_loss_bps, "stop loss", 10000)
        if (risk.entries_allowed is not True or cost > risk.cash_rials
                or risk.open_purchase_cost_rials + cost > cap):
            raise ValueError("paper entry constraints are not met")
        quoted_at = timestamp(quote.quoted_at)
        cutoff = timestamp(instrument.expires_at)
        deadline = min(quoted_at + MAX_QUOTE_AGE, cutoff - timedelta(days=HOLD_DAYS))
        if not now < deadline:
            raise ValueError("proposal already expired")
        key = digest(canonical([instrument.ins_code, iso(quoted_at)]))
        payload = {
            "scope": SCOPE, "symbol": instrument.symbol, "ins_code": instrument.ins_code,
            "option_type": instrument.option_type, "quantity": quantity,
            "contract_multiplier": instrument.contract_size, "premium_rials_per_unit": quote.ask,
            "maximum_purchase_cost_rials": cost,
            "rank_fingerprint": digest(canonical(ranked)),
            "risk": {**asdict(risk), "allocation_limit_percent": ALLOCATION_PERCENT,
                     "allocation_limit_rials": cap, "max_holding_calendar_days": HOLD_DAYS,
                     "contract_trade_cutoff_utc": cutoff.isoformat()},
            "quote": {"source": instrument.source, "evidence_ref": evidence_ref,
                      "exchange_quoted_at_utc": quoted_at.isoformat(),
                      "received_at_utc": timestamp(quote.received_at).isoformat(),
                      "bid_rials_per_unit": quote.bid, "ask_depth": ranked["ask_volume"],
                      "valid_until_utc": deadline.isoformat()},
        }
        return cls(key, canonical(payload), deadline)

    @property
    def fingerprint(self):
        return digest(self.payload)


SCHEMA = (
    """CREATE TABLE requests (
        id TEXT PRIMARY KEY NOT NULL, proposal_key TEXT NOT NULL UNIQUE,
        payload TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
        valid_until TEXT NOT NULL, CHECK(created_at < valid_until)
    )""",
    """CREATE TABLE audit (
        request_id TEXT NOT NULL REFERENCES requests(id), at TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED')),
        actor TEXT NOT NULL CHECK(actor IN ('system','operator')), reason TEXT NOT NULL,
        PRIMARY KEY (request_id, state),
        CHECK ((state IN ('PENDING','EXPIRED') AND actor = 'system')
            OR (state IN ('APPROVED','REJECTED') AND actor = 'operator')
            OR state = 'CANCELLED')
    )""",
    """CREATE UNIQUE INDEX one_terminal ON audit(request_id) WHERE state <> 'PENDING'""",
    """CREATE TRIGGER audit_transition BEFORE INSERT ON audit WHEN NOT EXISTS (
        SELECT 1 FROM requests r WHERE r.id = NEW.request_id AND NEW.at >= r.created_at
        AND ((NEW.state = 'PENDING' AND NEW.at = r.created_at)
          OR (NEW.state <> 'PENDING' AND EXISTS (
                SELECT 1 FROM audit WHERE request_id = r.id AND state = 'PENDING')
              AND ((NEW.state = 'EXPIRED' AND NEW.at >= r.valid_until)
                OR (NEW.state <> 'EXPIRED' AND NEW.at < r.valid_until))))
        ) BEGIN SELECT RAISE(ABORT, 'invalid approval transition'); END""",
    """CREATE TRIGGER requests_no_replace BEFORE INSERT ON requests WHEN EXISTS (
           SELECT 1 FROM requests WHERE id = NEW.id OR proposal_key = NEW.proposal_key
       ) BEGIN SELECT RAISE(ABORT, 'request already exists'); END""",
    """CREATE TRIGGER audit_no_replace BEFORE INSERT ON audit WHEN
           (NEW.state <> 'PENDING' AND NOT EXISTS (
               SELECT 1 FROM audit WHERE request_id = NEW.request_id AND state = 'PENDING'
           )) OR EXISTS (
               SELECT 1 FROM audit WHERE request_id = NEW.request_id
                 AND (state = NEW.state OR (state <> 'PENDING' AND NEW.state <> 'PENDING'))
           )
       BEGIN SELECT RAISE(ABORT, 'invalid or duplicate state transition'); END""",
    *(f"""CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
          BEGIN SELECT RAISE(ABORT, 'approval records are immutable'); END"""
      for table in ("requests", "audit") for action in ("UPDATE", "DELETE")),
)

WITH_STATE = """SELECT r.*, COALESCE(a.state, 'PENDING') AS state
                FROM requests r LEFT JOIN audit a ON a.request_id = r.id AND a.state <> 'PENDING'"""


class ApprovalStore:
    def __init__(self, path, eligible, *, clock=utc_now):
        self.path = Path(path)
        self.eligible = eligible
        self.clock = clock

    @contextmanager
    def _transaction(self, *, initialize=False, write=False):
        mode = "rw" if write else "ro"
        with closing(sqlite3.connect(f"{self.path.absolute().as_uri()}?mode={mode}", uri=True)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA recursive_triggers=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if initialize and (app_id, version) == (0, 0) and not db.execute(
                "SELECT 1 FROM sqlite_master LIMIT 1"
            ).fetchone():
                for sql in SCHEMA:
                    db.execute(sql)
                db.execute(f"PRAGMA application_id={APP_ID}")
                db.execute("PRAGMA user_version=1")
            elif (app_id, version) != (APP_ID, 1):
                raise ValueError("not a Phase 1 approval database")
            yield db

    def initialize(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder = self.path.parent.lstat()
        if not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.getuid() or folder.st_mode & 0o077:
            raise ValueError("approval state directory must be private and owned by the operator")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ValueError("unsafe approval database path")
        else:
            os.close(fd)
        with self._transaction(initialize=True, write=True):
            pass

    @staticmethod
    def _audit(db, request_id, now, state, actor, reason):
        db.execute("INSERT INTO audit VALUES (?, ?, ?, ?, ?)",
                   (request_id, iso(now), state, actor, reason))

    def _current(self):
        proposals = {}
        for proposal in self.eligible():
            if proposal.key in proposals:
                raise ValueError("ambiguous proposal identity")
            proposals[proposal.key] = proposal
        # Sample after reading eligibility and after acquiring the write lock.
        now = timestamp(self.clock())
        return now, {key: p for key, p in proposals.items() if now < p.valid_until}

    def current_fingerprints(self):
        """Read-only view; decisions independently revalidate under the write lock."""
        _, current = self._current()
        return {key: proposal.fingerprint for key, proposal in current.items()}

    def _invalidate(self, db, row, now, current):
        if now < timestamp(row["created_at"]):
            return "clock_error"
        if now >= timestamp(row["valid_until"]):
            self._audit(db, row["id"], now, "EXPIRED", "system", "deadline_elapsed")
            return "expired"
        proposal = current.get(row["proposal_key"])
        if proposal is None or not secrets.compare_digest(proposal.fingerprint, row["fingerprint"]):
            self._audit(db, row["id"], now, "CANCELLED", "system", "eligibility_withdrawn_or_changed")
            return "changed"
        return None

    def reconcile(self):
        """Ingest current trusted proposals and close stale requests, atomically."""
        with self._transaction(write=True) as db:
            now, current = self._current()
            for row in db.execute(WITH_STATE + " WHERE a.request_id IS NULL").fetchall():
                self._invalidate(db, row, now, current)
            for proposal in current.values():
                if db.execute("SELECT 1 FROM requests WHERE proposal_key = ?", (proposal.key,)).fetchone():
                    continue
                request_id = secrets.token_hex(16)
                db.execute(
                    "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                    (request_id, proposal.key, proposal.payload, proposal.fingerprint,
                     iso(now), iso(proposal.valid_until)),
                )
                self._audit(db, request_id, now, "PENDING", "system", "eligible_snapshot")

    def decide(self, request_id, fingerprint, decision):
        if decision not in DECISIONS:
            raise ValueError("invalid operator decision")
        with self._transaction(write=True) as db:
            row = db.execute(WITH_STATE + " WHERE r.id = ?", (request_id,)).fetchone()
            if row is None:
                return "missing"
            if row["state"] != "PENDING":
                return "already_closed"
            if not secrets.compare_digest(fingerprint, row["fingerprint"]) or digest(row["payload"]) != fingerprint:
                return "wrong_snapshot"
            now, current = self._current()
            invalid = self._invalidate(db, row, now, current)
            if invalid:
                return invalid
            self._audit(db, request_id, now, decision, "operator", "explicit_paper_research_decision")
            return "recorded"

    def recent(self, limit=50):
        with self._transaction() as db:
            return db.execute(WITH_STATE + """
                ORDER BY a.request_id IS NOT NULL, r.created_at DESC, r.id LIMIT ?
            """, (limit,)).fetchall()

    def detail(self, request_id):
        with self._transaction() as db:
            row = db.execute(WITH_STATE + " WHERE r.id = ?", (request_id,)).fetchone()
            events = db.execute("""SELECT at, actor, state, reason FROM audit WHERE request_id=?
                                   ORDER BY state <> 'PENDING'""", (request_id,)).fetchall()
            return row, events
