"""SQLite persistence for FINN. Schema v4 (v3 + announcements)."""
from __future__ import annotations

import math
import numbers
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from portfolio import PORTFOLIO

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "finn.sqlite3"
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
    symbol TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS thresholds (
    symbol TEXT PRIMARY KEY REFERENCES portfolio(symbol),
    price_pct REAL NOT NULL DEFAULT 5.0 CHECK (price_pct > 0),
    volume_multiple REAL NOT NULL DEFAULT 2.0 CHECK (volume_multiple > 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol TEXT NOT NULL REFERENCES portfolio(symbol),
    session_date TEXT NOT NULL,
    adjusted_close REAL,
    volume REAL,
    price_field TEXT NOT NULL,
    fetched_at TEXT,
    PRIMARY KEY (symbol, session_date)
);
CREATE TABLE IF NOT EXISTS evaluated_sessions (
    symbol TEXT NOT NULL REFERENCES portfolio(symbol),
    session_date TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('alerted', 'seeded')),
    evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, session_date)
);
CREATE TABLE IF NOT EXISTS session_checks (
    symbol TEXT NOT NULL REFERENCES portfolio(symbol),
    session_date TEXT NOT NULL,
    check_name TEXT NOT NULL CHECK (check_name IN ('price', 'volume')),
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, session_date, check_name)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL REFERENCES portfolio(symbol),
    session_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('price_up', 'price_down', 'volume_spike')),
    observed REAL NOT NULL,
    threshold REAL NOT NULL,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol, session_date, kind)
);
CREATE INDEX IF NOT EXISTS idx_alerts_session ON alerts(session_date DESC, symbol);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL REFERENCES portfolio(symbol),
    source TEXT NOT NULL DEFAULT 'nse',
    source_id TEXT NOT NULL,
    announced_at TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT,
    attachment_url TEXT,
    category TEXT NOT NULL,
    materiality TEXT NOT NULL CHECK (materiality IN ('high', 'medium', 'low')),
    matched_rule TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE (symbol, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_announcements_time ON announcements(announced_at DESC, symbol);
"""

EXPECTED_COLUMNS = {
    "portfolio": {"symbol", "active"},
    "thresholds": {"symbol", "price_pct", "volume_multiple", "updated_at"},
    "daily_bars": {"symbol", "session_date", "adjusted_close", "volume",
                   "price_field", "fetched_at"},
    "evaluated_sessions": {"symbol", "session_date", "mode", "evaluated_at"},
    "session_checks": {"symbol", "session_date", "check_name", "completed_at"},
    "alerts": {"id", "symbol", "session_date", "kind", "observed", "threshold",
               "evidence", "created_at"},
    "announcements": {"id", "symbol", "source", "source_id", "announced_at", "subject",
                      "description", "attachment_url", "category", "materiality",
                      "matched_rule", "ingested_at"},
}
V3_TABLES = {"portfolio", "thresholds", "daily_bars", "evaluated_sessions",
             "session_checks", "alerts"}


def _open(db_path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(db_path=DB_PATH):
    """Read-write transaction: commit on success, roll back on any error, always close."""
    conn = _open(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read_session(db_path=DB_PATH):
    """Read-only connection; yields None if the file does not exist. Creates nothing."""
    path = Path(db_path)
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def inspect_db(db_path=DB_PATH) -> dict:
    """Read-only description of an existing database. Never writes."""
    with read_session(db_path) as conn:
        if conn is None:
            return {"exists": False}
        return {
            "exists": True,
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "tables": sorted(_table_names(conn)),
            "compatible": _schema_problems(conn) == []
            and conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION,
        }


def _schema_problems(conn, tables=None):
    problems = []
    have = _table_names(conn)
    for table, cols in EXPECTED_COLUMNS.items():
        if tables is not None and table not in tables:
            continue
        if table not in have:
            problems.append(f"missing table {table}")
            continue
        found = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if found != cols:
            problems.append(f"{table}: columns {sorted(found)} != expected {sorted(cols)}")
    return problems


def initialize(db_path=DB_PATH, symbols=PORTFOLIO):
    """Create or validate the schema. Never overwrites or deletes an existing database."""
    with session(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if _table_names(conn) and version == 3:
            # Additive v3 -> v4 migration: only if the v3 schema is exactly intact.
            # It only creates the announcements table; existing data is not touched.
            v3_problems = _schema_problems(conn, V3_TABLES)
            if v3_problems or "announcements" in _table_names(conn):
                raise RuntimeError(f"{db_path} looks like v3 but is not a clean v3 schema "
                                   f"({v3_problems}). Left untouched; rename it to migrate.")
        elif _table_names(conn) and version != SCHEMA_VERSION:
            raise RuntimeError(
                f"{db_path} has an incompatible schema (user_version={version}, "
                f"expected {SCHEMA_VERSION}). It was left untouched; rename it to migrate.")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        problems = _schema_problems(conn)
        if problems:
            raise RuntimeError(f"Schema validation failed: {problems}")
        for s in symbols:
            s = s.strip().upper()
            conn.execute("INSERT OR IGNORE INTO portfolio(symbol) VALUES (?)", (s,))
            conn.execute("INSERT OR IGNORE INTO thresholds(symbol) VALUES (?)", (s,))


def _positive_finite(name, value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{name} must be a number")
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return v


def set_thresholds(symbol, price_pct=None, volume_multiple=None, db_path=DB_PATH):
    """Affects only checks not yet completed; completed checks are never re-run."""
    symbol = symbol.strip().upper()
    p = _positive_finite("price_pct", price_pct)
    v = _positive_finite("volume_multiple", volume_multiple)
    with session(db_path) as conn:
        if conn.execute("SELECT 1 FROM thresholds WHERE symbol=?", (symbol,)).fetchone() is None:
            raise ValueError(f"Unknown portfolio symbol: {symbol}")
        conn.execute(
            """UPDATE thresholds
               SET price_pct = COALESCE(?, price_pct),
                   volume_multiple = COALESCE(?, volume_multiple),
                   updated_at = CURRENT_TIMESTAMP
               WHERE symbol = ?""",
            (p, v, symbol))
