
import sqlite3
from pathlib import Path

DB_PATH = Path("data/trading.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = ON")

conn.executescript("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    symbol TEXT NOT NULL,
    option_type TEXT NOT NULL CHECK(option_type IN ('CALL', 'PUT')),
    underlying_symbol TEXT,
    entry_price REAL,
    target_price REAL,
    stop_loss REAL,
    expiry_date TEXT,
    max_holding_days INTEGER NOT NULL DEFAULT 7,
    score REAL,
    status TEXT NOT NULL DEFAULT 'NEW',
    reason TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    option_type TEXT NOT NULL CHECK(option_type IN ('CALL', 'PUT')),
    underlying_symbol TEXT,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    entry_price REAL NOT NULL CHECK(entry_price > 0),
    entry_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    initial_stop REAL,
    current_stop REAL,
    target_price REAL,
    highest_price REAL,
    expiry_date TEXT,
    max_holding_days INTEGER NOT NULL DEFAULT 7,
    status TEXT NOT NULL DEFAULT 'OPEN',
    exit_price REAL,
    exit_at TEXT,
    exit_reason TEXT,
    notes TEXT,
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    event_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    price REAL,
    message TEXT,
    FOREIGN KEY(trade_id) REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_signals_status
ON signals(status);

CREATE INDEX IF NOT EXISTS idx_trades_status
ON trades(status);

CREATE INDEX IF NOT EXISTS idx_events_trade
ON trade_events(trade_id);
""")

conn.commit()

print("Database initialized successfully.")
print("Location:", DB_PATH.resolve())

for table in ("signals", "trades", "trade_events"):
    count = conn.execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]
    print(f"{table}: {count} records")

conn.close()
