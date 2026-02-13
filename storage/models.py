"""SQL schema definitions and row-mapping helpers for the trade database."""

from __future__ import annotations

# ── Schema DDL ───────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- Signals received from Telegram (raw + parsed)
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_hash     TEXT    UNIQUE NOT NULL,
    signal_type     TEXT    NOT NULL,          -- ENTRY / EXIT / BOOK_PROFIT
    underlying      TEXT    NOT NULL,
    expiry_day      INTEGER,
    expiry_month    TEXT,
    strike_price    REAL,
    option_type     TEXT,                      -- CE / PE
    entry_price     REAL,
    exit_price      REAL,
    targets         TEXT,                      -- JSON array
    stoploss        REAL,
    raw_text        TEXT,
    telegram_msg_id INTEGER,
    received_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    processed       INTEGER NOT NULL DEFAULT 0 -- 0=pending, 1=processed, -1=skipped
);

-- Orders placed (real or paper)
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id),
    order_ref       TEXT,                      -- Groww order ID or paper-trade ref
    trading_symbol  TEXT    NOT NULL,
    groww_symbol    TEXT,
    exchange        TEXT    NOT NULL DEFAULT 'NSE',
    segment         TEXT    NOT NULL DEFAULT 'FNO',
    product         TEXT    NOT NULL DEFAULT 'NRML',
    transaction_type TEXT   NOT NULL,          -- BUY / SELL
    order_type      TEXT    NOT NULL,          -- MARKET / LIMIT / SL / SL_M
    quantity        INTEGER NOT NULL,
    price           REAL,
    trigger_price   REAL,
    status          TEXT    NOT NULL DEFAULT 'PENDING',
    filled_qty      INTEGER DEFAULT 0,
    avg_fill_price  REAL,
    is_paper        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Open positions (aggregated view)
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id),
    underlying      TEXT    NOT NULL,
    trading_symbol  TEXT    NOT NULL,
    groww_symbol    TEXT,
    option_type     TEXT,
    strike_price    REAL,
    expiry_day      INTEGER,
    expiry_month    TEXT,
    quantity        INTEGER NOT NULL DEFAULT 0,
    avg_entry_price REAL    NOT NULL,
    stoploss        REAL,
    targets         TEXT,                      -- JSON array
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN / CLOSED
    pnl             REAL    DEFAULT 0,
    is_paper        INTEGER NOT NULL DEFAULT 1,
    opened_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT
);

-- Processed signal hashes for idempotency
CREATE TABLE IF NOT EXISTS processed_signals (
    signal_hash     TEXT    PRIMARY KEY,
    processed_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_signals_hash ON signals(signal_hash);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_underlying ON positions(underlying);
CREATE INDEX IF NOT EXISTS idx_orders_signal ON orders(signal_id);
"""
