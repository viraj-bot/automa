"""Backtest engine — replays historical Telegram messages through the parser
and simulates trades using Groww historical candle data for realistic fills.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from config.settings import Settings
from parser.models import (
    BookProfitSignal,
    EntrySignal,
    ExitSignal,
    TradeSignal,
)
from parser.signal_parser import SignalParser
from storage.db import Database
from telegram.history import fetch_chat_history

logger = logging.getLogger(__name__)

# Month abbreviation → number for datetime construction
_MONTH_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_MONTH_TITLE = {
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
}


class BacktestEngine:
    """Replay historical Telegram signals against Groww historical data.

    Workflow
    --------
    1. Fetch chat history from Telegram.
    2. Parse each message through ``SignalParser``.
    3. For ENTRY signals, look up the actual candle price at that timestamp
       via ``groww.get_historical_candles()`` and simulate a fill.
    4. For EXIT / BOOK_PROFIT signals, close the simulated position using
       the historical price.
    5. Generate a summary report.
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db
        self._parser = SignalParser()
        self._groww: Any = None  # lazy import to avoid hard dep in paper-only mode

    # ── Public API ───────────────────────────────────────────────────────

    async def run(self, days: int = 30, limit: Optional[int] = None) -> dict[str, Any]:
        """Execute the full backtest and return a summary dict."""
        # 1. Fetch history
        messages = await fetch_chat_history(self._settings, days=days, limit=limit)
        if not messages:
            logger.warning("No messages fetched — nothing to backtest")
            return {"total_signals": 0}

        logger.info("Backtesting %d messages over %d days …", len(messages), days)

        # 2. Optionally initialise Groww for historical price lookups
        self._init_groww()

        # 3. Replay
        parsed_count = 0
        skipped = 0

        for msg in messages:
            signal = self._parser.parse(
                msg["text"],
                message_id=msg["id"],
                timestamp=msg["date"],
            )
            if signal is None:
                continue

            parsed_count += 1

            # Idempotency
            if await self._db.is_signal_processed(signal.signal_hash):
                skipped += 1
                continue

            signal_id = await self._db.insert_signal(signal)

            try:
                await self._process_signal(signal, signal_id, msg["date"])
            except Exception:
                logger.exception("Error processing signal %s", signal.signal_hash)

            await self._db.mark_signal_processed(signal.signal_hash)

        # 4. Summary
        summary = await self._db.get_trade_summary()
        summary["total_messages"] = len(messages)
        summary["total_signals"] = parsed_count
        summary["skipped_duplicates"] = skipped

        return summary

    # ── Internal ─────────────────────────────────────────────────────────

    def _init_groww(self) -> None:
        """Lazily create a GrowwAPI instance for historical data."""
        try:
            from growwapi import GrowwAPI

            self._groww = GrowwAPI(self._settings.groww_api_token)
            logger.info("Groww API initialised for backtest historical data")
        except Exception:
            logger.warning(
                "Could not initialise Groww API — backtest will use signal prices"
            )
            self._groww = None

    def _get_historical_price(
        self,
        underlying: str,
        expiry_day: int,
        expiry_month: str,
        strike_price: float,
        option_type: str,
        at_time: datetime,
    ) -> Optional[float]:
        """Fetch the close price of a 1-min candle nearest to *at_time*."""
        if self._groww is None:
            return None

        month_title = _MONTH_TITLE.get(expiry_month.upper(), expiry_month.title())
        year_short = at_time.year % 100

        groww_symbol = (
            f"NSE-{underlying}-{expiry_day:02d}{month_title}{year_short}"
            f"-{int(strike_price)}-{option_type}"
        )

        start = at_time.strftime("%Y-%m-%d %H:%M:%S")
        end = (at_time + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

        try:
            data = self._groww.get_historical_candles(
                exchange=self._groww.EXCHANGE_NSE,
                segment=self._groww.SEGMENT_FNO,
                groww_symbol=groww_symbol,
                start_time=start,
                end_time=end,
                candle_interval=self._groww.CANDLE_INTERVAL_MIN_1,
            )
            candles = data.get("candles", [])
            if candles:
                # Return the close price of the first candle
                return float(candles[0][4])
        except Exception:
            logger.debug("Could not fetch historical candle for %s", groww_symbol)

        return None

    async def _process_signal(
        self,
        signal: TradeSignal,
        signal_id: int,
        msg_time: datetime,
    ) -> None:
        """Simulate a trade for the given signal."""
        if isinstance(signal, EntrySignal):
            await self._simulate_entry(signal, signal_id, msg_time)
        elif isinstance(signal, ExitSignal):
            await self._simulate_exit(signal, signal_id, msg_time)
        elif isinstance(signal, BookProfitSignal):
            await self._simulate_book_profit(signal, signal_id, msg_time)

    async def _simulate_entry(
        self, signal: EntrySignal, signal_id: int, msg_time: datetime
    ) -> None:
        # Try to get the actual market price at the time of the signal
        loop = asyncio.get_running_loop()
        hist_price = await loop.run_in_executor(
            None,
            lambda: self._get_historical_price(
                signal.underlying,
                signal.expiry_day,
                signal.expiry_month,
                signal.strike_price,
                signal.option_type.value,
                msg_time,
            ),
        )

        fill_price = hist_price if hist_price is not None else signal.entry_price
        quantity = self._settings.default_lot_multiplier  # simplified for backtest

        ts = (
            f"{signal.underlying}{signal.expiry_day:02d}"
            f"{signal.expiry_month[:3]}{int(signal.strike_price)}"
            f"{signal.option_type.value}"
        )

        await self._db.open_position(
            signal_id=signal_id,
            underlying=signal.underlying,
            trading_symbol=ts,
            quantity=quantity,
            avg_entry_price=fill_price,
            option_type=signal.option_type.value,
            strike_price=signal.strike_price,
            expiry_day=signal.expiry_day,
            expiry_month=signal.expiry_month,
            stoploss=signal.stoploss,
            targets=signal.targets,
            is_paper=True,
        )

        logger.info(
            "[BT] ENTRY %s x%d @ ₹%.2f (signal: ₹%.2f)",
            signal.display_name, quantity, fill_price, signal.entry_price,
        )

    async def _simulate_exit(
        self, signal: ExitSignal, signal_id: int, msg_time: datetime
    ) -> None:
        position = await self._db.find_open_position(
            underlying=signal.underlying,
            strike_price=signal.strike_price,
            option_type=signal.option_type.value if signal.option_type else None,
            expiry_day=signal.expiry_day,
            expiry_month=signal.expiry_month,
        )
        if position is None:
            positions = await self._db.find_all_open_positions_for_underlying(
                signal.underlying
            )
            if not positions:
                logger.debug("[BT] No open position for EXIT %s", signal.display_name)
                return
            position = positions[0]

        # Try historical price
        loop = asyncio.get_running_loop()
        hist_price = None
        if position.get("strike_price") and position.get("option_type"):
            hist_price = await loop.run_in_executor(
                None,
                lambda: self._get_historical_price(
                    position["underlying"],
                    position.get("expiry_day", 1),
                    position.get("expiry_month", "JAN"),
                    position["strike_price"],
                    position["option_type"],
                    msg_time,
                ),
            )

        exit_price = hist_price if hist_price is not None else position["avg_entry_price"]
        pnl = (exit_price - position["avg_entry_price"]) * position["quantity"]
        await self._db.close_position(position["id"], pnl=pnl)

        logger.info(
            "[BT] EXIT %s @ ₹%.2f  P&L: ₹%.2f",
            position["trading_symbol"], exit_price, pnl,
        )

    async def _simulate_book_profit(
        self, signal: BookProfitSignal, signal_id: int, msg_time: datetime
    ) -> None:
        position = await self._db.find_open_position(
            underlying=signal.underlying,
            strike_price=signal.strike_price,
            option_type=signal.option_type.value if signal.option_type else None,
            expiry_day=signal.expiry_day,
            expiry_month=signal.expiry_month,
        )
        if position is None:
            positions = await self._db.find_all_open_positions_for_underlying(
                signal.underlying
            )
            if not positions:
                logger.debug("[BT] No open position for BOOK_PROFIT %s", signal.display_name)
                return
            position = positions[0]

        if signal.exit_price is not None:
            exit_price = signal.exit_price
        else:
            # Try historical
            loop = asyncio.get_running_loop()
            hist_price = None
            if position.get("strike_price") and position.get("option_type"):
                hist_price = await loop.run_in_executor(
                    None,
                    lambda: self._get_historical_price(
                        position["underlying"],
                        position.get("expiry_day", 1),
                        position.get("expiry_month", "JAN"),
                        position["strike_price"],
                        position["option_type"],
                        msg_time,
                    ),
                )
            exit_price = hist_price if hist_price is not None else position["avg_entry_price"]

        pnl = (exit_price - position["avg_entry_price"]) * position["quantity"]
        await self._db.close_position(position["id"], pnl=pnl)

        logger.info(
            "[BT] BOOK_PROFIT %s @ ₹%.2f  P&L: ₹%.2f",
            position["trading_symbol"], exit_price, pnl,
        )
