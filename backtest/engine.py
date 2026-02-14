"""Backtest engine — replays historical Telegram messages through the parser
and simulates trades using Groww historical candle data for realistic fills.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

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

_MONTH_TITLE = {
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
}



class BacktestEngine:
    """Replay historical Telegram signals against Groww historical data.

    Each backtest run uses a **fresh in-memory database** so that repeated
    runs don't see stale "already processed" markers.

    Workflow
    --------
    1. Fetch chat history from Telegram.
    2. Parse each message through ``SignalParser``.
    3. For ENTRY signals, record the position at the signal's entry price
       (or the actual candle price if Groww historical data is available).
    4. For EXIT / BOOK_PROFIT signals, close the matching position.
       - If the signal contains an explicit exit price, use it.
       - Otherwise try the Groww historical candle price.
       - As a last resort, use the first target from the entry signal
         (optimistic estimate) rather than defaulting to entry price.
    5. After all messages are replayed, force-close any remaining open
       positions at their stoploss (worst case) to give a realistic picture.
    6. Generate a summary report.
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db
        self._parser = SignalParser()
        self._groww: Any = None
        self._instruments_df: Optional[pd.DataFrame] = None
        self._lot_size_cache: Optional[dict[str, int]] = None  # underlying → lot size

        # ── Trade log: every closed trade is recorded here ──
        self._trade_log: list[dict[str, Any]] = []

        # Map position trading_symbol → entry message time (for the trade log)
        self._entry_times: dict[str, str] = {}

        # Counters
        self._unmatched_close_signals = 0
        self._unmatched_exit_signals = 0
        self._unmatched_bp_signals = 0

    # ── Public API ───────────────────────────────────────────────────────

    async def run(self, days: int = 30, limit: Optional[int] = None) -> dict[str, Any]:
        """Execute the full backtest and return a summary dict."""

        # Use a fresh in-memory DB so repeated runs start clean
        bt_db = Database(":memory:")
        await bt_db.connect()
        self._db = bt_db

        # 1. Fetch history
        messages = await fetch_chat_history(self._settings, days=days, limit=limit)
        if not messages:
            logger.warning("No messages fetched — nothing to backtest")
            await bt_db.close()
            return {"total_signals": 0, "total_messages": 0}

        logger.info("Backtesting %d messages over %d days …", len(messages), days)

        # 2. Optionally initialise Groww for historical price lookups
        self._init_groww()

        # 3. Replay
        parsed_count = 0
        entry_count = 0
        exit_count = 0
        bp_count = 0
        errors = 0
        entries_with_targets = 0
        entries_without_targets = 0
        bp_with_price = 0
        bp_without_price = 0

        unparsed_count = 0
        for msg in messages:
            signal = self._parser.parse(
                msg["text"],
                message_id=msg["id"],
                timestamp=msg["date"],
            )
            if signal is None:
                # Log unparsed messages so we can improve the parser
                preview = msg["text"].replace("\n", " ").strip()
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                logger.warning(
                    "[UNPARSED] msg_id=%s date=%s | %s",
                    msg["id"],
                    msg["date"].strftime("%Y-%m-%d %H:%M") if hasattr(msg["date"], "strftime") else msg["date"],
                    preview,
                )
                unparsed_count += 1
                continue

            parsed_count += 1
            signal_id = await self._db.insert_signal(signal)

            try:
                await self._process_signal(signal, signal_id, msg["date"])
                if isinstance(signal, EntrySignal):
                    entry_count += 1
                    if signal.targets:
                        entries_with_targets += 1
                    else:
                        entries_without_targets += 1
                    # Always log entry details for debugging
                    logger.info(
                        "[BT] ENTRY %s | price=₹%.2f | SL=%s | targets=%s | raw: %s",
                        signal.display_name, signal.entry_price,
                        signal.stoploss, signal.targets,
                        repr(signal.raw_text[:200]),
                    )
                elif isinstance(signal, ExitSignal):
                    exit_count += 1
                elif isinstance(signal, BookProfitSignal):
                    bp_count += 1
                    if signal.exit_price is not None:
                        bp_with_price += 1
                    else:
                        bp_without_price += 1
            except Exception:
                logger.exception("Error processing signal")
                errors += 1

        # 4. Log open positions before force-closing (diagnostic)
        pre_close_open = await self._db.get_all_positions(status="OPEN")
        if pre_close_open:
            logger.info(
                "[BT] %d positions still open before force-close:",
                len(pre_close_open),
            )
            for p in pre_close_open:
                logger.info(
                    "  -> id=%s underlying=%r ts=%s strike=%s otype=%s "
                    "day=%s month=%s entry=₹%.2f targets=%s sl=%s",
                    p["id"], p["underlying"], p["trading_symbol"],
                    p.get("strike_price"), p.get("option_type"),
                    p.get("expiry_day"), p.get("expiry_month"),
                    p["avg_entry_price"], p.get("targets"), p.get("stoploss"),
                )

        # Force-close orphaned open positions at stoploss or entry price
        await self._close_orphaned_positions()

        # 5. Build summary from the trade log
        trades = self._trade_log
        total_trades = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] < 0)
        breakeven = sum(1 for t in trades if t["pnl"] == 0)
        total_pnl = sum(t["pnl"] for t in trades)
        avg_pnl = total_pnl / total_trades if total_trades else 0
        best_trade = max((t["pnl"] for t in trades), default=0)
        worst_trade = min((t["pnl"] for t in trades), default=0)

        # Price source counts from the trade log
        entry_from_groww = sum(1 for t in trades if t["entry_source"] == "groww")
        entry_from_signal = sum(1 for t in trades if t["entry_source"] == "signal")
        exit_from_groww = sum(1 for t in trades if t["exit_source"] == "groww")
        exit_from_signal = sum(1 for t in trades if t["exit_source"] == "signal")
        exit_from_target = sum(1 for t in trades if t["exit_source"] == "target")
        exit_from_stoploss = sum(1 for t in trades if t["exit_source"] == "stoploss")
        exit_from_entry = sum(1 for t in trades if t["exit_source"] == "entry")

        summary: dict[str, Any] = {
            "total_messages": len(messages),
            "total_signals": parsed_count,
            "entries": entry_count,
            "exits": exit_count,
            "book_profits": bp_count,
            "errors": errors,
            "unparsed_messages": unparsed_count,
            "entries_with_targets": entries_with_targets,
            "entries_without_targets": entries_without_targets,
            "bp_with_price": bp_with_price,
            "bp_without_price": bp_without_price,
            # Trade stats (computed from trade log)
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            # Price sources
            "entry_from_groww": entry_from_groww,
            "entry_from_signal": entry_from_signal,
            "exit_from_groww": exit_from_groww,
            "exit_from_signal": exit_from_signal,
            "exit_from_target": exit_from_target,
            "exit_from_stoploss": exit_from_stoploss,
            "exit_from_entry": exit_from_entry,
            # Unmatched
            "unmatched_close_signals": self._unmatched_close_signals,
            "unmatched_exit_signals": self._unmatched_exit_signals,
            "unmatched_bp_signals": self._unmatched_bp_signals,
            # The full trade log for per-trade reporting
            "trade_log": trades,
        }

        await bt_db.close()
        return summary

    # ── Internal ─────────────────────────────────────────────────────────

    def _init_groww(self) -> None:
        """Initialise Groww API for historical data and load instruments CSV.

        The instruments CSV (lot sizes) is loaded first — it's a public
        download that doesn't require authentication.  Then we attempt the
        API key + secret exchange for an access token needed by authenticated
        endpoints (historical candles, etc.).
        """
        from growwapi import GrowwAPI

        # Step 1: Load instruments CSV (public, no auth needed)
        # Build a lot-size cache: underlying_symbol → lot_size
        try:
            tmp_api = GrowwAPI(self._settings.groww_api_token)
            self._instruments_df = tmp_api.get_all_instruments()
            logger.info(
                "Loaded %d instruments from Groww",
                len(self._instruments_df),
            )

            # Build lot-size cache by underlying symbol
            df = self._instruments_df
            if "underlying_symbol" in df.columns and "lot_size" in df.columns:
                cache: dict[str, int] = {}
                for _, row in df[["underlying_symbol", "lot_size"]].drop_duplicates(
                    subset=["underlying_symbol"]
                ).iterrows():
                    sym = str(row["underlying_symbol"]).upper()
                    lot = int(row["lot_size"])
                    if lot > 0:
                        cache[sym] = lot
                self._lot_size_cache = cache
                logger.info(
                    "Built lot-size cache for %d underlyings (e.g. NIFTY=%s, BANKNIFTY=%s)",
                    len(cache),
                    cache.get("NIFTY", "?"),
                    cache.get("BANKNIFTY", "?"),
                )
        except Exception:
            logger.warning(
                "Could not load instruments CSV — lot sizes will default to 1",
                exc_info=True,
            )

        # Step 2: Exchange API key + secret for access token (authenticated)
        try:
            access_token = GrowwAPI.get_access_token(
                api_key=self._settings.groww_api_token,
                secret=self._settings.groww_api_secret,
            )
            self._groww = GrowwAPI(access_token)
            logger.info("Groww API authenticated — historical candle data available")
        except Exception:
            logger.warning(
                "Could not obtain Groww access token — backtest will use signal "
                "prices instead of historical candles. Check GROWW_API_SECRET.",
                exc_info=True,
            )
            self._groww = None

    def _resolve_lot_size(
        self,
        underlying: str,
        expiry_day: int,
        expiry_month: str,
        strike_price: float,
        option_type: str,
        at_time: datetime,
    ) -> int:
        """Look up the lot size for an instrument from the Groww instruments CSV.

        The lot size is the same for all instruments of a given underlying,
        so we look up by underlying_symbol rather than exact groww_symbol
        (which may not exist for expired instruments).

        Returns the lot size from Groww if available, otherwise falls back to 1.
        """
        # Try the cached lot-size map first (built from instruments CSV)
        if self._lot_size_cache is not None:
            lot = self._lot_size_cache.get(underlying.upper())
            if lot is not None:
                return lot

        logger.warning(
            "[BT] Lot size not found for %s, defaulting to 1", underlying,
        )
        return 1

    def _build_groww_symbol(
        self,
        underlying: str,
        expiry_day: int,
        expiry_month: str,
        strike_price: float,
        option_type: str,
        at_time: datetime,
    ) -> str:
        """Build the Groww symbol string for an F&O instrument."""
        month_title = _MONTH_TITLE.get(expiry_month.upper(), expiry_month.title())
        year_short = at_time.year % 100
        return (
            f"NSE-{underlying}-{expiry_day:02d}{month_title}{year_short}"
            f"-{int(strike_price)}-{option_type}"
        )

    def _get_historical_price(
        self,
        underlying: str,
        expiry_day: int,
        expiry_month: str,
        strike_price: float,
        option_type: str,
        at_time: datetime,
    ) -> Optional[float]:
        """Fetch the close price of a candle nearest to *at_time*.

        Tries progressively wider time windows:
        1.  ±5 minutes around the signal time  (1-min candles)
        2.  ±30 minutes                        (1-min candles)
        3.  Full trading day 09:15–15:30        (5-min candles)
        """
        if self._groww is None:
            return None

        groww_symbol = self._build_groww_symbol(
            underlying, expiry_day, expiry_month,
            strike_price, option_type, at_time,
        )

        # ── Strategy 1: tight window ±5 min, 1-min candles ──
        price = self._fetch_candle_price(
            groww_symbol, at_time,
            window_before=timedelta(minutes=5),
            window_after=timedelta(minutes=5),
            interval=self._groww.CANDLE_INTERVAL_MIN_1,
        )
        if price is not None:
            return price

        # ── Strategy 2: wider window ±30 min, 1-min candles ──
        price = self._fetch_candle_price(
            groww_symbol, at_time,
            window_before=timedelta(minutes=30),
            window_after=timedelta(minutes=30),
            interval=self._groww.CANDLE_INTERVAL_MIN_1,
        )
        if price is not None:
            return price

        # ── Strategy 3: full trading day, 5-min candles ──
        trading_day = at_time.replace(hour=9, minute=15, second=0, microsecond=0)
        trading_end = at_time.replace(hour=15, minute=30, second=0, microsecond=0)
        price = self._fetch_candle_price(
            groww_symbol, at_time,
            explicit_start=trading_day,
            explicit_end=trading_end,
            interval=self._groww.CANDLE_INTERVAL_MIN_5,
        )
        if price is not None:
            return price

        logger.warning(
            "[GROWW] No candle data for %s at %s (tried 3 strategies)",
            groww_symbol, at_time.strftime("%Y-%m-%d %H:%M"),
        )
        return None

    def _fetch_candle_price(
        self,
        groww_symbol: str,
        target_time: datetime,
        *,
        window_before: Optional[timedelta] = None,
        window_after: Optional[timedelta] = None,
        explicit_start: Optional[datetime] = None,
        explicit_end: Optional[datetime] = None,
        interval: str = "1minute",
    ) -> Optional[float]:
        """Low-level helper: fetch candles and return the close price of the
        candle nearest to *target_time*.
        """
        if explicit_start and explicit_end:
            start_dt = explicit_start
            end_dt = explicit_end
        else:
            start_dt = target_time - (window_before or timedelta(minutes=5))
            end_dt = target_time + (window_after or timedelta(minutes=5))

        start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

        logger.debug(
            "[GROWW REQ] candles %s  %s → %s  interval=%s",
            groww_symbol, start_str, end_str, interval,
        )

        try:
            data = self._groww.get_historical_candles(
                exchange=self._groww.EXCHANGE_NSE,
                segment=self._groww.SEGMENT_FNO,
                groww_symbol=groww_symbol,
                start_time=start_str,
                end_time=end_str,
                candle_interval=interval,
            )
            candles = data.get("candles", [])
            logger.debug(
                "[GROWW RES] %s: %d candles returned", groww_symbol, len(candles),
            )

            if not candles:
                return None

            # Find the candle closest to target_time
            target_ts = target_time.timestamp()
            best_candle = None
            best_diff = float("inf")
            for c in candles:
                # candle[0] is the timestamp (could be epoch ms or string)
                candle_ts = c[0]
                if isinstance(candle_ts, str):
                    try:
                        candle_ts = datetime.strptime(
                            candle_ts, "%Y-%m-%d %H:%M:%S"
                        ).timestamp()
                    except ValueError:
                        candle_ts = float(candle_ts) / 1000  # epoch ms
                elif candle_ts > 1e12:
                    candle_ts = candle_ts / 1000  # epoch ms → seconds

                diff = abs(candle_ts - target_ts)
                if diff < best_diff:
                    best_diff = diff
                    best_candle = c

            if best_candle is not None:
                price = float(best_candle[4])  # close price
                logger.debug(
                    "[GROWW] %s nearest candle close=₹%.2f (%.0fs from signal)",
                    groww_symbol, price, best_diff,
                )
                return price

        except Exception as exc:
            logger.debug(
                "[GROWW ERR] candles %s: %s", groww_symbol, exc,
            )

        return None

    async def _process_signal(
        self,
        signal: TradeSignal,
        signal_id: int,
        msg_time: datetime,
    ) -> None:
        if isinstance(signal, EntrySignal):
            await self._simulate_entry(signal, signal_id, msg_time)
        elif isinstance(signal, ExitSignal):
            await self._simulate_exit(signal, signal_id, msg_time)
        elif isinstance(signal, BookProfitSignal):
            await self._simulate_book_profit(signal, signal_id, msg_time)

    async def _simulate_entry(
        self, signal: EntrySignal, signal_id: int, msg_time: datetime
    ) -> None:
        loop = asyncio.get_running_loop()
        hist_price = await loop.run_in_executor(
            None,
            lambda: self._get_historical_price(
                signal.underlying, signal.expiry_day, signal.expiry_month,
                signal.strike_price, signal.option_type.value, msg_time,
            ),
        )

        if hist_price is not None:
            fill_price = hist_price
            entry_source = "groww"
        else:
            fill_price = signal.entry_price
            entry_source = "signal"

        # Look up the real lot size from the instruments CSV
        lot_size = self._resolve_lot_size(
            signal.underlying, signal.expiry_day, signal.expiry_month,
            signal.strike_price, signal.option_type.value, msg_time,
        )
        quantity = lot_size * self._settings.default_lot_multiplier

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

        # Remember the entry message time for the trade log
        entry_time_str = msg_time.strftime("%Y-%m-%d %H:%M") if hasattr(msg_time, "strftime") else str(msg_time)
        self._entry_times[ts] = entry_time_str

        logger.info(
            "[BT] ENTRY %s x%d @ ₹%.2f (%s) | signal=₹%.2f SL=%s T=%s",
            signal.display_name, quantity, fill_price, entry_source,
            signal.entry_price, signal.stoploss, signal.targets,
        )

    async def _simulate_exit(
        self, signal: ExitSignal, signal_id: int, msg_time: datetime
    ) -> None:
        position = await self._find_matching_position(signal)
        if position is None:
            logger.warning("[BT] No open position for EXIT %s", signal.display_name)
            self._unmatched_close_signals += 1
            self._unmatched_exit_signals += 1
            return

        entry_price = float(position["avg_entry_price"])
        quantity = int(position["quantity"])

        # Prefer the explicit exit price from the signal (e.g. "at the current price ₹72.5")
        if signal.exit_price is not None:
            exit_price = float(signal.exit_price)
            exit_source = "signal"
        else:
            exit_price, exit_source = await self._resolve_exit_price(
                position, msg_time, close_reason="EXIT",
            )

        pnl = (exit_price - entry_price) * quantity
        await self._db.close_position(position["id"], pnl=pnl)

        exit_time_str = msg_time.strftime("%Y-%m-%d %H:%M") if hasattr(msg_time, "strftime") else str(msg_time)
        self._trade_log.append({
            "trade_no": len(self._trade_log) + 1,
            "instrument": position["trading_symbol"],
            "underlying": position["underlying"],
            "qty": quantity,
            "entry_price": entry_price,
            "entry_source": "signal",
            "exit_price": exit_price,
            "exit_source": exit_source,
            "close_type": "EXIT",
            "pnl": pnl,
            "entry_time": self._entry_times.get(position["trading_symbol"], ""),
            "exit_time": exit_time_str,
        })

        logger.info(
            "[BT] EXIT %s  entry=₹%.2f  exit=₹%.2f (%s)  P&L=₹%.2f",
            position["trading_symbol"], entry_price, exit_price, exit_source, pnl,
        )

    async def _simulate_book_profit(
        self, signal: BookProfitSignal, signal_id: int, msg_time: datetime
    ) -> None:
        position = await self._find_matching_position(signal)
        if position is None:
            # Position was already closed (likely by an earlier EXIT signal).
            # If this book-profit has an explicit exit price, try to update
            # the recently closed position's P&L with the better price.
            if signal.exit_price is not None:
                updated = await self._update_closed_position_pnl(signal)
                if updated:
                    return
            logger.warning(
                "[BT] No open position for BOOK_PROFIT %s (exit_price=₹%s)",
                signal.display_name, signal.exit_price,
            )
            self._unmatched_close_signals += 1
            self._unmatched_bp_signals += 1
            return

        entry_price = float(position["avg_entry_price"])
        quantity = int(position["quantity"])

        # Book profit signals often include an explicit exit price
        if signal.exit_price is not None:
            exit_price = float(signal.exit_price)
            exit_source = "signal"  # explicit price from the message
        else:
            exit_price, exit_source = await self._resolve_exit_price(
                position, msg_time, close_reason="BOOK_PROFIT",
            )

        # ── Handle partial vs full book-profit ──
        if signal.is_partial:
            # Close ~50% of the position, keep the rest open
            close_qty = quantity // 2
            if close_qty <= 0:
                close_qty = quantity  # if only 1 lot, close it all

            # Round to lot size if possible
            lot_size = self._resolve_lot_size(
                position["underlying"],
                position.get("expiry_day") or 1,
                position.get("expiry_month") or "JAN",
                position.get("strike_price") or 0,
                position.get("option_type") or "CE",
                msg_time,
            )
            if lot_size > 1 and close_qty >= lot_size:
                close_qty = (close_qty // lot_size) * lot_size

            remaining_qty = quantity - close_qty
            partial_pnl = (exit_price - entry_price) * close_qty

            if remaining_qty > 0:
                # Partial close: reduce quantity, keep position open
                await self._db.partial_close_position(
                    position["id"],
                    close_qty=close_qty,
                    partial_pnl=partial_pnl,
                )
                close_type = "PARTIAL"
            else:
                # Closing everything (only 1 lot or rounding ate it all)
                await self._db.close_position(position["id"], pnl=partial_pnl)
                close_type = "BOOK_PROFIT"

            exit_time_str = msg_time.strftime("%Y-%m-%d %H:%M") if hasattr(msg_time, "strftime") else str(msg_time)
            self._trade_log.append({
                "trade_no": len(self._trade_log) + 1,
                "instrument": position["trading_symbol"],
                "underlying": position["underlying"],
                "qty": close_qty,
                "entry_price": entry_price,
                "entry_source": "signal",
                "exit_price": exit_price,
                "exit_source": exit_source,
                "close_type": close_type,
                "pnl": partial_pnl,
                "entry_time": self._entry_times.get(position["trading_symbol"], ""),
                "exit_time": exit_time_str,
            })

            logger.info(
                "[BT] PARTIAL_BP %s  closed %d/%d @ ₹%.2f (%s)  P&L=₹%.2f  remaining=%d",
                position["trading_symbol"], close_qty, quantity,
                exit_price, exit_source, partial_pnl, remaining_qty,
            )
        else:
            # Full book-profit: close the entire position
            pnl = (exit_price - entry_price) * quantity
            await self._db.close_position(position["id"], pnl=pnl)

            exit_time_str = msg_time.strftime("%Y-%m-%d %H:%M") if hasattr(msg_time, "strftime") else str(msg_time)
            self._trade_log.append({
                "trade_no": len(self._trade_log) + 1,
                "instrument": position["trading_symbol"],
                "underlying": position["underlying"],
                "qty": quantity,
                "entry_price": entry_price,
                "entry_source": "signal",
                "exit_price": exit_price,
                "exit_source": exit_source,
                "close_type": "BOOK_PROFIT",
                "pnl": pnl,
                "entry_time": self._entry_times.get(position["trading_symbol"], ""),
                "exit_time": exit_time_str,
            })

            logger.info(
                "[BT] BOOK_PROFIT %s  entry=₹%.2f  exit=₹%.2f (%s)  P&L=₹%.2f",
                position["trading_symbol"], entry_price, exit_price, exit_source, pnl,
            )

    async def _update_closed_position_pnl(
        self, signal: BookProfitSignal
    ) -> bool:
        """When a book-profit signal arrives but the position is already closed
        (by an earlier EXIT), update the trade log with the real exit price.

        Returns True if a matching closed position was found and updated.
        """
        sig_otype = signal.option_type.value if signal.option_type else None
        closed_pos = await self._db.find_recently_closed_position(
            underlying=signal.underlying,
            strike_price=signal.strike_price,
            option_type=sig_otype,
        )
        if closed_pos is None:
            return False

        entry_price = float(closed_pos["avg_entry_price"])
        quantity = int(closed_pos["quantity"])
        new_exit = float(signal.exit_price)
        new_pnl = (new_exit - entry_price) * quantity

        # Update the DB
        await self._db.update_position_pnl(closed_pos["id"], new_pnl)

        # Update the trade log entry for this position
        for trade in self._trade_log:
            if (trade["instrument"] == closed_pos["trading_symbol"]
                    and trade["entry_price"] == entry_price):
                old_pnl = trade["pnl"]
                trade["exit_price"] = new_exit
                trade["exit_source"] = "signal"
                trade["close_type"] = "BOOK_PROFIT"
                trade["pnl"] = new_pnl
                logger.info(
                    "[BT] UPDATED %s: exit ₹%.2f→₹%.2f (%s→signal) P&L ₹%.2f→₹%.2f",
                    closed_pos["trading_symbol"],
                    trade.get("exit_price", 0), new_exit,
                    trade.get("exit_source", "?"),
                    old_pnl, new_pnl,
                )
                return True

        return False

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _find_matching_position(self, signal: ExitSignal | BookProfitSignal) -> Optional[dict]:
        """Find the best matching open position for an exit/book-profit signal.

        Tries progressively broader matching:
        1. Exact match on all available fields (underlying, strike, otype, expiry)
        2. Match on underlying + strike + option_type only
        3. Match on underlying + option_type only
        4. Match on underlying only
        """
        sig_underlying = signal.underlying
        sig_strike = signal.strike_price
        sig_otype = signal.option_type.value if signal.option_type else None
        sig_day = signal.expiry_day
        sig_month = signal.expiry_month

        # 1. Exact match with all available fields
        position = await self._db.find_open_position(
            underlying=sig_underlying,
            strike_price=sig_strike,
            option_type=sig_otype,
            expiry_day=sig_day,
            expiry_month=sig_month,
        )
        if position is not None:
            logger.debug(
                "[BT] MATCH (exact) for %s: pos_id=%s ts=%s",
                signal.display_name, position["id"], position["trading_symbol"],
            )
            return position

        # 2. Match on underlying + strike + option_type (ignore expiry)
        if sig_strike is not None and sig_otype is not None:
            position = await self._db.find_open_position(
                underlying=sig_underlying,
                strike_price=sig_strike,
                option_type=sig_otype,
            )
            if position is not None:
                logger.debug(
                    "[BT] MATCH (strike+otype) for %s: pos_id=%s ts=%s",
                    signal.display_name, position["id"], position["trading_symbol"],
                )
                return position

        # 3. Match on underlying + option_type only
        if sig_otype is not None:
            position = await self._db.find_open_position(
                underlying=sig_underlying,
                option_type=sig_otype,
            )
            if position is not None:
                logger.debug(
                    "[BT] MATCH (underlying+otype) for %s: pos_id=%s ts=%s",
                    signal.display_name, position["id"], position["trading_symbol"],
                )
                return position

        # 4. Broadest: match on underlying only
        positions = await self._db.find_all_open_positions_for_underlying(sig_underlying)
        if positions:
            logger.debug(
                "[BT] MATCH (underlying only) for %s: pos_id=%s ts=%s",
                signal.display_name, positions[0]["id"], positions[0]["trading_symbol"],
            )
            return positions[0]

        # No match found — log all open positions for debugging
        all_open = await self._db.get_all_positions(status="OPEN")
        if all_open:
            open_underlyings = [
                f"{p['underlying']}({p['trading_symbol']})" for p in all_open
            ]
            logger.warning(
                "[BT] NO MATCH for %s (underlying=%r strike=%s otype=%s day=%s month=%s). "
                "Open positions: %s",
                signal.display_name, sig_underlying, sig_strike, sig_otype,
                sig_day, sig_month, ", ".join(open_underlyings),
            )
        else:
            logger.warning(
                "[BT] NO MATCH for %s — no open positions at all",
                signal.display_name,
            )
        return None

    async def _resolve_exit_price(
        self, position: dict, msg_time: datetime,
        *, close_reason: str = "EXIT",
    ) -> tuple[float, str]:
        """Determine the exit price for a position.

        Returns ``(price, source)`` where *source* is one of:
        ``"groww"``, ``"target"``, ``"stoploss"``, ``"entry"``.

        Fallback logic:
        - BOOK_PROFIT / EXIT: groww → target → stoploss → entry
        - ORPHAN:             groww → stoploss → entry
        """
        ts = position.get("trading_symbol", "?")
        entry = float(position["avg_entry_price"])

        # ── 1. Try historical price from Groww ──
        if position.get("strike_price") and position.get("option_type"):
            loop = asyncio.get_running_loop()
            hist_price = await loop.run_in_executor(
                None,
                lambda: self._get_historical_price(
                    position["underlying"],
                    position.get("expiry_day") or 1,
                    position.get("expiry_month") or "JAN",
                    position["strike_price"],
                    position["option_type"],
                    msg_time,
                ),
            )
            if hist_price is not None:
                return hist_price, "groww"

        # ── 2. Parse helpers ──
        targets_raw = position.get("targets")
        first_target = None
        if targets_raw:
            try:
                targets = json.loads(targets_raw) if isinstance(targets_raw, str) else targets_raw
                if targets and len(targets) > 0:
                    first_target = float(targets[0])
            except (json.JSONDecodeError, TypeError, IndexError):
                pass

        stoploss = position.get("stoploss")
        stoploss_val = float(stoploss) if stoploss is not None and float(stoploss) > 0 else None

        # ── 3. Fallback depends on close_reason ──
        if close_reason == "BOOK_PROFIT":
            # Admin explicitly said "book profit" → trade was profitable.
            # Use target as best estimate.
            if first_target is not None:
                logger.info(
                    "[BT] Exit %s (BOOK_PROFIT): target ₹%.2f (no Groww data)",
                    ts, first_target,
                )
                return first_target, "target"

        elif close_reason == "EXIT":
            # Admin said "exit" without saying "book profit".
            # In this group, profitable exits get a BOOK_PROFIT signal.
            # A plain EXIT likely means the trade didn't work out → use stoploss.
            # (If a BOOK_PROFIT arrives later, _update_closed_position_pnl
            #  will correct the P&L with the real exit price.)
            if stoploss_val is not None:
                logger.info(
                    "[BT] Exit %s (EXIT): stoploss ₹%.2f (no Groww data, "
                    "assuming loss — will be corrected if book-profit follows)",
                    ts, stoploss_val,
                )
                return stoploss_val, "stoploss"

        else:
            # ORPHAN — assume worst case
            if stoploss_val is not None:
                logger.info(
                    "[BT] Exit %s (ORPHAN): stoploss ₹%.2f",
                    ts, stoploss_val,
                )
                return stoploss_val, "stoploss"

        logger.warning(
            "[BT] Exit %s (%s): entry ₹%.2f (no data at all)",
            ts, close_reason, entry,
        )
        return entry, "entry"

    async def _close_orphaned_positions(self) -> None:
        """Force-close positions that were never explicitly exited."""
        open_positions = await self._db.get_all_positions(status="OPEN")
        if not open_positions:
            return

        logger.info(
            "Force-closing %d orphaned positions (no exit signal received)",
            len(open_positions),
        )

        for pos in open_positions:
            entry = float(pos["avg_entry_price"])
            quantity = int(pos["quantity"])
            stoploss = pos.get("stoploss")

            if stoploss is not None and float(stoploss) > 0:
                exit_price = float(stoploss)
                exit_source = "stoploss"
            else:
                exit_price = entry
                exit_source = "entry"

            pnl = (exit_price - entry) * quantity
            await self._db.close_position(pos["id"], pnl=pnl)

            self._trade_log.append({
                "trade_no": len(self._trade_log) + 1,
                "instrument": pos["trading_symbol"],
                "underlying": pos["underlying"],
                "qty": quantity,
                "entry_price": entry,
                "entry_source": "signal",
                "exit_price": exit_price,
                "exit_source": exit_source,
                "close_type": "ORPHAN",
                "pnl": pnl,
                "entry_time": self._entry_times.get(pos["trading_symbol"], ""),
                "exit_time": "force-closed",
            })

            logger.info(
                "[BT] FORCE-CLOSE %s  entry=₹%.2f  exit=₹%.2f (%s)  P&L=₹%.2f",
                pos["trading_symbol"], entry, exit_price, exit_source, pnl,
            )
