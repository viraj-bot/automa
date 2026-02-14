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

        # Counters for exit-price source tracking
        self._exit_from_groww = 0
        self._exit_from_target = 0       # book-profit exits using target price
        self._exit_from_stoploss = 0     # exits/orphans using stoploss price
        self._exit_from_entry_fallback = 0  # last-resort break-even at entry
        self._unmatched_close_signals = 0

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

        # 5. Summary
        summary = await self._db.get_trade_summary()
        summary["total_messages"] = len(messages)
        summary["total_signals"] = parsed_count
        summary["entries"] = entry_count
        summary["exits"] = exit_count
        summary["book_profits"] = bp_count
        summary["errors"] = errors

        # Count still-open positions (shouldn't be any after force-close)
        open_positions = await self._db.get_all_positions(status="OPEN")
        summary["orphaned_positions"] = len(open_positions)
        summary["unparsed_messages"] = unparsed_count

        # Exit-price source breakdown
        summary["exit_from_groww"] = self._exit_from_groww
        summary["exit_from_target"] = self._exit_from_target
        summary["exit_from_stoploss"] = self._exit_from_stoploss
        summary["exit_from_entry_fallback"] = self._exit_from_entry_fallback
        summary["unmatched_close_signals"] = self._unmatched_close_signals

        # Entry quality breakdown
        summary["entries_with_targets"] = entries_with_targets
        summary["entries_without_targets"] = entries_without_targets
        summary["bp_with_price"] = bp_with_price
        summary["bp_without_price"] = bp_without_price

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
        try:
            tmp_api = GrowwAPI(self._settings.groww_api_token)
            self._instruments_df = tmp_api.get_all_instruments()
            logger.info(
                "Loaded %d instruments (lot-size lookups enabled)",
                len(self._instruments_df),
            )
        except Exception:
            logger.warning(
                "Could not load instruments CSV — lot sizes will default to 1",
                exc_info=True,
            )
            self._instruments_df = None

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
        """Look up the lot size for an instrument from the instruments CSV.

        Returns the lot size from Groww if available, otherwise falls back to 1.
        The final quantity is ``lot_size * default_lot_multiplier``.
        """
        if self._instruments_df is None:
            return 1

        month_title = _MONTH_TITLE.get(expiry_month.upper(), expiry_month.title())
        year_short = at_time.year % 100

        groww_symbol = (
            f"NSE-{underlying}-{expiry_day:02d}{month_title}{year_short}"
            f"-{int(strike_price)}-{option_type}"
        )

        df = self._instruments_df
        match = df[df["groww_symbol"] == groww_symbol]
        if not match.empty:
            lot = int(match.iloc[0].get("lot_size", 1))
            logger.debug(
                "[BT] Lot size for %s: %d", groww_symbol, lot,
            )
            return max(lot, 1)

        # Fuzzy fallback: match by underlying + strike + option type
        if "underlying_symbol" in df.columns:
            mask = (
                (df["underlying_symbol"] == underlying)
                & (df["strike_price"] == strike_price)
                & (df["instrument_type"] == option_type)
            )
            fuzzy = df[mask]
            if not fuzzy.empty:
                lot = int(fuzzy.iloc[0].get("lot_size", 1))
                logger.debug(
                    "[BT] Lot size for %s (fuzzy): %d", groww_symbol, lot,
                )
                return max(lot, 1)

        logger.debug("[BT] Lot size not found for %s, defaulting to 1", groww_symbol)
        return 1

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

        req_params = {
            "groww_symbol": groww_symbol,
            "start_time": start,
            "end_time": end,
            "interval": "1min",
        }
        logger.debug("[GROWW REQ] get_historical_candles: %s", req_params)

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
            logger.debug(
                "[GROWW RES] get_historical_candles %s: %d candles, data=%s",
                groww_symbol, len(candles), data,
            )
            if candles:
                return float(candles[0][4])  # close price
        except Exception as exc:
            logger.debug(
                "[GROWW ERR] get_historical_candles %s: %s", groww_symbol, exc
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

        fill_price = hist_price if hist_price is not None else signal.entry_price

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

        logger.info(
            "[BT] ENTRY %s x%d @ ₹%.2f (signal: ₹%.2f, SL: %s, T: %s)",
            signal.display_name, quantity, fill_price, signal.entry_price,
            signal.stoploss, signal.targets,
        )

    async def _simulate_exit(
        self, signal: ExitSignal, signal_id: int, msg_time: datetime
    ) -> None:
        logger.debug(
            "[BT] Looking for EXIT match: underlying=%r strike=%s otype=%s day=%s month=%s",
            signal.underlying, signal.strike_price,
            signal.option_type.value if signal.option_type else None,
            signal.expiry_day, signal.expiry_month,
        )
        position = await self._find_matching_position(signal)
        if position is None:
            logger.warning("[BT] No open position for EXIT %s", signal.display_name)
            self._unmatched_close_signals += 1
            return

        exit_price = await self._resolve_exit_price(
            position, msg_time, close_reason="EXIT",
        )
        pnl = (exit_price - position["avg_entry_price"]) * position["quantity"]
        await self._db.close_position(position["id"], pnl=pnl)

        logger.info(
            "[BT] EXIT %s  entry=₹%.2f  exit=₹%.2f  P&L=₹%.2f",
            position["trading_symbol"], position["avg_entry_price"], exit_price, pnl,
        )

    async def _simulate_book_profit(
        self, signal: BookProfitSignal, signal_id: int, msg_time: datetime
    ) -> None:
        logger.debug(
            "[BT] Looking for BOOK_PROFIT match: underlying=%r strike=%s otype=%s day=%s month=%s exit_price=%s",
            signal.underlying, signal.strike_price,
            signal.option_type.value if signal.option_type else None,
            signal.expiry_day, signal.expiry_month, signal.exit_price,
        )
        position = await self._find_matching_position(signal)
        if position is None:
            logger.warning("[BT] No open position for BOOK_PROFIT %s", signal.display_name)
            self._unmatched_close_signals += 1
            return

        # Book profit signals often include an explicit exit price
        if signal.exit_price is not None:
            exit_price = signal.exit_price
        else:
            exit_price = await self._resolve_exit_price(
                position, msg_time, close_reason="BOOK_PROFIT",
            )

        pnl = (exit_price - position["avg_entry_price"]) * position["quantity"]
        await self._db.close_position(position["id"], pnl=pnl)

        logger.info(
            "[BT] BOOK_PROFIT %s  entry=₹%.2f  exit=₹%.2f  P&L=₹%.2f",
            position["trading_symbol"], position["avg_entry_price"], exit_price, pnl,
        )

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
    ) -> float:
        """Determine the exit price for a position, trying multiple sources.

        ``close_reason`` controls fallback behaviour when no market data is
        available:

        * ``"BOOK_PROFIT"`` — the signal explicitly says profit was booked,
          so using the first target is a reasonable estimate.
        * ``"EXIT"`` — a plain exit (could be a stop-loss hit), so we
          conservatively use the stoploss as the fallback.
        * ``"ORPHAN"`` — position was never explicitly closed; assume worst
          case (stoploss).

        Priority (common):
        1. Groww historical candle price at the exit time.

        Then, depending on close_reason:
        - BOOK_PROFIT: target → stoploss → entry
        - EXIT:        stoploss → entry
        - ORPHAN:      stoploss → entry
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
                self._exit_from_groww += 1
                return hist_price

        # ── 2. Fallback depends on close_reason ──

        # Parse helpers
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

        if close_reason == "BOOK_PROFIT":
            # Signal says profit was booked → target is a fair estimate
            if first_target is not None:
                self._exit_from_target += 1
                logger.info(
                    "[BT] Exit price for %s (BOOK_PROFIT): using first target ₹%.2f",
                    ts, first_target,
                )
                return first_target
            # No target available — try stoploss as conservative fallback
            if stoploss_val is not None:
                self._exit_from_stoploss += 1
                logger.info(
                    "[BT] Exit price for %s (BOOK_PROFIT): no target, using stoploss ₹%.2f",
                    ts, stoploss_val,
                )
                return stoploss_val

        else:
            # EXIT or ORPHAN — trade may have gone wrong; use stoploss
            if stoploss_val is not None:
                self._exit_from_stoploss += 1
                logger.info(
                    "[BT] Exit price for %s (%s): using stoploss ₹%.2f "
                    "(no historical data)",
                    ts, close_reason, stoploss_val,
                )
                return stoploss_val

        # ── 3. Absolute last resort: break-even at entry price ──
        self._exit_from_entry_fallback += 1
        logger.warning(
            "[BT] Exit price for %s (%s): falling back to entry price ₹%.2f "
            "(no historical data, no stoploss)",
            ts, close_reason, entry,
        )
        return entry

    async def _close_orphaned_positions(self) -> None:
        """Force-close any positions that were never explicitly exited.

        Priority for exit price:
        1. Stoploss (worst-case scenario)
        2. First target (optimistic estimate if no stoploss)
        3. Entry price (break-even, last resort)
        """
        open_positions = await self._db.get_all_positions(status="OPEN")
        if not open_positions:
            return

        logger.info(
            "Force-closing %d orphaned positions (no exit signal received)",
            len(open_positions),
        )

        for pos in open_positions:
            entry = pos["avg_entry_price"]
            stoploss = pos.get("stoploss")
            source = "entry (break-even)"

            if stoploss is not None and float(stoploss) > 0:
                # Assume worst case — hit stoploss (no exit signal = likely bad outcome)
                exit_price = float(stoploss)
                source = "stoploss"
                self._exit_from_stoploss += 1
            else:
                # No stoploss available — break-even at entry
                exit_price = entry
                self._exit_from_entry_fallback += 1

            pnl = (exit_price - entry) * pos["quantity"]
            await self._db.close_position(pos["id"], pnl=pnl)

            logger.info(
                "[BT] FORCE-CLOSE %s  entry=₹%.2f  exit=₹%.2f (%s)  P&L=₹%.2f",
                pos["trading_symbol"], entry, exit_price, source, pnl,
            )
