"""SQLite persistence: watchlist, strategy decisions, paper orders,
backtest results. All writes are guarded by a lock (same pattern as the
Prism bot) so the scheduler thread and Telegram handlers never collide.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT PRIMARY KEY,
    added_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    action  TEXT NOT NULL,   -- buy | sell | hold
    price   REAL,
    reason  TEXT,
    mode    TEXT NOT NULL    -- mock | paper-api
);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol_ts ON decisions(symbol, ts);

CREATE TABLE IF NOT EXISTS paper_orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        REAL NOT NULL,
    price      REAL,
    status     TEXT NOT NULL,
    broker_id  TEXT,
    mode       TEXT NOT NULL  -- mock | paper-api
);

CREATE TABLE IF NOT EXISTS backtests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    params         TEXT,
    total_return_pct REAL,
    max_drawdown_pct REAL,
    win_rate_pct     REAL,
    trades           INTEGER,
    final_equity     REAL
);

-- Panel book: the Moon-style LLM panel's OWN paper account, kept fully
-- separate from the strategy book so the two track records can be
-- compared honestly (see bot/panelbook.py).
CREATE TABLE IF NOT EXISTS panel_positions (
    symbol           TEXT PRIMARY KEY,
    qty              REAL NOT NULL,
    avg_price        REAL NOT NULL,
    opened_ts        TEXT NOT NULL,
    entry_confidence REAL,
    entry_votes      TEXT
);

CREATE TABLE IF NOT EXISTS panel_trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT NOT NULL,
    opened_ts        TEXT NOT NULL,
    closed_ts        TEXT NOT NULL,
    qty              REAL NOT NULL,
    entry_price      REAL NOT NULL,
    exit_price       REAL NOT NULL,
    pnl              REAL NOT NULL,
    entry_confidence REAL,
    exit_reason      TEXT
);

CREATE TABLE IF NOT EXISTS panel_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        log.info("Database ready at %s", path)

    # ------------------------------------------------------------ watchlist
    def seed_watchlist(self, symbols: list[str]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO watchlist(symbol, added_ts) VALUES (?, ?)",
                [(s.upper(), _now()) for s in symbols],
            )
            self._conn.commit()

    def list_watchlist(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol FROM watchlist ORDER BY symbol"
            ).fetchall()
        return [r["symbol"] for r in rows]

    def add_to_watchlist(self, symbol: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO watchlist(symbol, added_ts) VALUES (?, ?)",
                (symbol.upper(), _now()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def remove_from_watchlist(self, symbol: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM watchlist WHERE symbol = ?", (symbol.upper(),)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------ decisions
    def log_decision(self, symbol: str, action: str, price: float, reason: str, mode: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions(ts, symbol, action, price, reason, mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), symbol.upper(), action, price, reason, mode),
            )
            self._conn.commit()

    def last_action(self, symbol: str) -> str | None:
        """Most recent non-hold action for a symbol (repeat-signal guard)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT action FROM decisions WHERE symbol = ? AND action != 'hold' "
                "ORDER BY id DESC LIMIT 1",
                (symbol.upper(),),
            ).fetchone()
        return row["action"] if row else None

    def recent_decisions(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    # ------------------------------------------------------------ orders
    def log_order(self, order: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO paper_orders(ts, symbol, side, qty, price, status, broker_id, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    order["symbol"],
                    order["side"],
                    order["qty"],
                    order.get("price"),
                    order["status"],
                    order.get("broker_id", ""),
                    order["mode"],
                ),
            )
            self._conn.commit()

    def order_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ panel book
    def panel_meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM panel_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def panel_meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO panel_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def panel_position(self, symbol: str):
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM panel_positions WHERE symbol = ?",
                (symbol.upper(),),
            ).fetchone()

    def panel_positions_all(self) -> list:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM panel_positions ORDER BY symbol"
            ).fetchall()

    def panel_open_position(
        self, symbol: str, qty: float, price: float,
        entry_confidence: float | None, entry_votes: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO panel_positions(symbol, qty, avg_price, opened_ts, "
                "entry_confidence, entry_votes) VALUES (?, ?, ?, ?, ?, ?)",
                (symbol.upper(), qty, price, _now(), entry_confidence, entry_votes),
            )
            self._conn.commit()

    def panel_close_position(self, symbol: str, exit_price: float, exit_reason: str):
        """Close the open position for symbol; returns the trade dict."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM panel_positions WHERE symbol = ?",
                (symbol.upper(),),
            ).fetchone()
            if row is None:
                return None
            pnl = (exit_price - row["avg_price"]) * row["qty"]
            closed_ts = _now()
            self._conn.execute(
                "INSERT INTO panel_trades(symbol, opened_ts, closed_ts, qty, "
                "entry_price, exit_price, pnl, entry_confidence, exit_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["symbol"], row["opened_ts"], closed_ts, row["qty"],
                    row["avg_price"], exit_price, pnl,
                    row["entry_confidence"], exit_reason,
                ),
            )
            self._conn.execute(
                "DELETE FROM panel_positions WHERE symbol = ?", (symbol.upper(),)
            )
            self._conn.commit()
        return {
            "symbol": row["symbol"], "qty": row["qty"],
            "entry_price": row["avg_price"], "exit_price": exit_price,
            "pnl": pnl, "opened_ts": row["opened_ts"], "closed_ts": closed_ts,
            "entry_confidence": row["entry_confidence"],
        }

    def panel_closed_trades(self) -> list:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM panel_trades ORDER BY id"
            ).fetchall()

    # ------------------------------------------------------------ backtests
    def log_backtest(self, result, params: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO backtests(ts, symbol, strategy, params, total_return_pct, "
                "max_drawdown_pct, win_rate_pct, trades, final_equity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    result.symbol,
                    result.strategy,
                    params,
                    result.total_return_pct,
                    result.max_drawdown_pct,
                    result.win_rate_pct,
                    len(result.trades),
                    result.final_equity,
                ),
            )
            self._conn.commit()
