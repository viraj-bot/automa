"""Async SQLite database layer for trade logging and state management."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import aiosqlite

from parser.models import (
    BookProfitSignal,
    EntrySignal,
    ExitSignal,
    TradeSignal,
)
from storage.models import SCHEMA_SQL

logger = logging.getLogger(__name__)


class Database:
    """Thin async wrapper around an SQLite database for the trade bot."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open the connection and ensure the schema exists."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
        logger.info("Database connected: %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    # ── Idempotency ──────────────────────────────────────────────────────

    async def is_signal_processed(self, signal_hash: str) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM processed_signals WHERE signal_hash = ?",
            (signal_hash,),
        )
        return (await cursor.fetchone()) is not None

    async def mark_signal_processed(self, signal_hash: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO processed_signals (signal_hash) VALUES (?)",
            (signal_hash,),
        )
        await self.conn.commit()

    # ── Signal logging ───────────────────────────────────────────────────

    async def insert_signal(self, signal: TradeSignal) -> int:
        """Insert a parsed signal and return its row ID."""
        if isinstance(signal, EntrySignal):
            cursor = await self.conn.execute(
                """INSERT INTO signals
                   (signal_hash, signal_type, underlying, expiry_day, expiry_month,
                    strike_price, option_type, entry_price, targets, stoploss,
                    raw_text, telegram_msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.signal_hash,
                    signal.signal_type.value,
                    signal.underlying,
                    signal.expiry_day,
                    signal.expiry_month,
                    signal.strike_price,
                    signal.option_type.value,
                    signal.entry_price,
                    json.dumps(signal.targets),
                    signal.stoploss,
                    signal.raw_text,
                    signal.message_id,
                ),
            )
        elif isinstance(signal, ExitSignal):
            cursor = await self.conn.execute(
                """INSERT INTO signals
                   (signal_hash, signal_type, underlying, expiry_day, expiry_month,
                    strike_price, option_type, raw_text, telegram_msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.signal_hash,
                    signal.signal_type.value,
                    signal.underlying,
                    signal.expiry_day,
                    signal.expiry_month,
                    signal.strike_price,
                    signal.option_type.value if signal.option_type else None,
                    signal.raw_text,
                    signal.message_id,
                ),
            )
        elif isinstance(signal, BookProfitSignal):
            cursor = await self.conn.execute(
                """INSERT INTO signals
                   (signal_hash, signal_type, underlying, expiry_day, expiry_month,
                    strike_price, option_type, exit_price, raw_text, telegram_msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.signal_hash,
                    signal.signal_type.value,
                    signal.underlying,
                    signal.expiry_day,
                    signal.expiry_month,
                    signal.strike_price,
                    signal.option_type.value if signal.option_type else None,
                    signal.exit_price,
                    signal.raw_text,
                    signal.message_id,
                ),
            )
        else:
            raise ValueError(f"Unknown signal type: {type(signal)}")

        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    # ── Order management ─────────────────────────────────────────────────

    async def insert_order(
        self,
        signal_id: int,
        trading_symbol: str,
        transaction_type: str,
        order_type: str,
        quantity: int,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        groww_symbol: Optional[str] = None,
        order_ref: Optional[str] = None,
        is_paper: bool = True,
        exchange: str = "NSE",
        segment: str = "FNO",
        product: str = "NRML",
    ) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO orders
               (signal_id, order_ref, trading_symbol, groww_symbol, exchange,
                segment, product, transaction_type, order_type, quantity,
                price, trigger_price, is_paper)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id,
                order_ref,
                trading_symbol,
                groww_symbol,
                exchange,
                segment,
                product,
                transaction_type,
                order_type,
                quantity,
                price,
                trigger_price,
                1 if is_paper else 0,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def update_order_status(
        self,
        order_id: int,
        status: str,
        filled_qty: int = 0,
        avg_fill_price: Optional[float] = None,
    ) -> None:
        await self.conn.execute(
            """UPDATE orders
               SET status = ?, filled_qty = ?, avg_fill_price = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (status, filled_qty, avg_fill_price, order_id),
        )
        await self.conn.commit()

    # ── Position management ──────────────────────────────────────────────

    async def open_position(
        self,
        signal_id: int,
        underlying: str,
        trading_symbol: str,
        quantity: int,
        avg_entry_price: float,
        option_type: Optional[str] = None,
        strike_price: Optional[float] = None,
        expiry_day: Optional[int] = None,
        expiry_month: Optional[str] = None,
        stoploss: Optional[float] = None,
        targets: Optional[list[float]] = None,
        groww_symbol: Optional[str] = None,
        is_paper: bool = True,
    ) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO positions
               (signal_id, underlying, trading_symbol, groww_symbol,
                option_type, strike_price, expiry_day, expiry_month,
                quantity, avg_entry_price, stoploss, targets, is_paper)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id,
                underlying,
                trading_symbol,
                groww_symbol,
                option_type,
                strike_price,
                expiry_day,
                expiry_month,
                quantity,
                avg_entry_price,
                stoploss,
                json.dumps(targets or []),
                1 if is_paper else 0,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def close_position(
        self, position_id: int, pnl: float
    ) -> None:
        await self.conn.execute(
            """UPDATE positions
               SET status = 'CLOSED', pnl = ?, closed_at = datetime('now')
               WHERE id = ?""",
            (pnl, position_id),
        )
        await self.conn.commit()

    async def find_open_position(
        self,
        underlying: str,
        strike_price: Optional[float] = None,
        option_type: Optional[str] = None,
        expiry_day: Optional[int] = None,
        expiry_month: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Find the best-matching open position for an exit/book-profit signal."""
        query = "SELECT * FROM positions WHERE status = 'OPEN' AND underlying = ?"
        params: list[Any] = [underlying]

        if strike_price is not None:
            query += " AND strike_price = ?"
            params.append(strike_price)
        if option_type is not None:
            query += " AND option_type = ?"
            params.append(option_type)
        if expiry_day is not None:
            query += " AND expiry_day = ?"
            params.append(expiry_day)
        if expiry_month is not None:
            query += " AND expiry_month = ?"
            params.append(expiry_month)

        query += " ORDER BY opened_at DESC LIMIT 1"

        cursor = await self.conn.execute(query, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_all_open_positions_for_underlying(
        self, underlying: str
    ) -> list[dict[str, Any]]:
        """Return all open positions for a given underlying."""
        cursor = await self.conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' AND underlying = ? ORDER BY opened_at DESC",
            (underlying,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_all_positions(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        if status:
            cursor = await self.conn.execute(
                "SELECT * FROM positions WHERE status = ? ORDER BY opened_at DESC",
                (status,),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM positions ORDER BY opened_at DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_trade_summary(self) -> dict[str, Any]:
        """Return aggregate statistics for closed positions.

        Breakeven trades (pnl = 0) are counted separately from losses.
        """
        cursor = await self.conn.execute(
            """SELECT
                 COUNT(*)                                   AS total_trades,
                 SUM(CASE WHEN pnl > 0  THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN pnl < 0  THEN 1 ELSE 0 END) AS losses,
                 SUM(CASE WHEN pnl = 0  THEN 1 ELSE 0 END) AS breakeven,
                 SUM(pnl)                                   AS total_pnl,
                 AVG(pnl)                                   AS avg_pnl,
                 MAX(pnl)                                   AS best_trade,
                 MIN(pnl)                                   AS worst_trade
               FROM positions WHERE status = 'CLOSED'"""
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}
