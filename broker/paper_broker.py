"""Paper-trading broker that simulates order execution without real money.

Uses the Groww instruments CSV for accurate lot sizes so that P&L
calculations reflect real-world quantities.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from broker.base import BrokerInterface
from config.settings import Settings
from parser.models import BookProfitSignal, EntrySignal, ExitSignal
from storage.db import Database

from config.log_config import TAG_PAPER, TAG_RISK

logger = logging.getLogger(__name__)

_MONTH_MAP = {
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
}


class PaperBroker(BrokerInterface):
    """Simulates trades in SQLite.  No real money is involved."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db
        self._instruments_df: Optional[pd.DataFrame] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Load the Groww instruments CSV for accurate lot sizes."""
        logger.info("%s Initialising — loading instruments for lot sizes …", TAG_PAPER)
        try:
            from growwapi import GrowwAPI

            api = GrowwAPI(self._settings.groww_api_token)
            loop = asyncio.get_running_loop()
            self._instruments_df = await loop.run_in_executor(
                None, api.get_all_instruments,
            )
            logger.info(
                "%s Loaded %d instruments (lot sizes available)",
                TAG_PAPER, len(self._instruments_df),
            )
        except Exception:
            logger.warning(
                "%s Could not load instruments CSV — paper trades will use lot_size=1",
                TAG_PAPER, exc_info=True,
            )
            self._instruments_df = None

    async def shutdown(self) -> None:
        logger.info("%s PaperBroker shut down", TAG_PAPER)

    # ── Instrument resolution (paper mode: passthrough) ──────────────────

    def resolve_trading_symbol(
        self,
        underlying: str,
        expiry_day: int,
        expiry_month: str,
        strike_price: float,
        option_type: str,
    ) -> Optional[dict[str, Any]]:
        """Build a trading symbol and look up the real lot size from Groww.

        If the instruments CSV is loaded, the lot size comes from the actual
        Groww data.  Otherwise it falls back to 1.
        """
        month_title = _MONTH_MAP.get(expiry_month[:3].upper(), expiry_month[:3].title())
        month_short = expiry_month[:3].upper()

        ts = f"{underlying}{expiry_day:02d}{month_short}{int(strike_price)}{option_type}"

        # Build groww_symbol for CSV lookup (e.g. NSE-NIFTY-17Feb26-25600-PE)
        now = datetime.now()
        candidates = [now.year % 100, (now.year + 1) % 100]
        lot_size = 1
        groww_sym = f"NSE-{underlying}-{expiry_day:02d}{month_title}{candidates[0]}-{int(strike_price)}-{option_type}"

        if self._instruments_df is not None:
            df = self._instruments_df
            for yy in candidates:
                gs = (
                    f"NSE-{underlying}-{expiry_day:02d}{month_title}{yy}"
                    f"-{int(strike_price)}-{option_type}"
                )
                match = df[df["groww_symbol"] == gs]
                if not match.empty:
                    lot_size = int(match.iloc[0].get("lot_size", 1))
                    groww_sym = gs
                    break
            else:
                # Fuzzy: match by underlying + strike + option type
                if "underlying_symbol" in df.columns:
                    mask = (
                        (df["underlying_symbol"] == underlying)
                        & (df["strike_price"] == strike_price)
                        & (df["instrument_type"] == option_type)
                    )
                    fuzzy = df[mask]
                    if not fuzzy.empty:
                        lot_size = int(fuzzy.iloc[0].get("lot_size", 1))
                        groww_sym = fuzzy.iloc[0]["groww_symbol"]

        # If CSV lookup didn't find a lot size, use the hardcoded fallback
        if lot_size <= 1:
            from backtest.engine import BacktestEngine
            lot_size = BacktestEngine._FALLBACK_LOT_SIZES.get(
                underlying.upper(), 1
            )

        lot_size = max(lot_size, 1)
        logger.debug("[PAPER] %s → lot_size=%d", groww_sym, lot_size)

        return {
            "trading_symbol": ts,
            "groww_symbol": groww_sym,
            "lot_size": lot_size,
        }

    # ── Entry ────────────────────────────────────────────────────────────

    async def execute_entry(self, signal: EntrySignal, signal_id: int) -> None:
        try:
            await self._execute_entry_inner(signal, signal_id)
        except Exception:
            logger.exception("%s Failed to execute entry for %s", TAG_PAPER, signal.display_name)
        finally:
            logger.info("")

    async def _execute_entry_inner(self, signal: EntrySignal, signal_id: int) -> None:
        instrument = self.resolve_trading_symbol(
            signal.underlying,
            signal.expiry_day,
            signal.expiry_month,
            signal.strike_price,
            signal.option_type.value,
        )
        if instrument is None:
            logger.warning("%s Could not resolve instrument for %s", TAG_PAPER, signal.display_name)
            return

        lot_size = max(instrument.get("lot_size", 1), 1)
        quantity = lot_size * self._settings.default_lot_multiplier

        stoploss = signal.stoploss
        if stoploss is None:
            stoploss = round(
                signal.entry_price * (1 - self._settings.default_sl_percent / 100), 2
            )
            logger.warning(
                "%s No SL in signal for %s — applying default %g%% SL @ ₹%.2f",
                TAG_RISK, signal.display_name, self._settings.default_sl_percent, stoploss,
            )

        risk = (signal.entry_price - stoploss) * quantity
        if risk > self._settings.max_risk_per_trade:
            logger.warning(
                "%s SKIP %s — risk ₹%.0f exceeds max ₹%.0f",
                TAG_RISK, signal.display_name, risk, self._settings.max_risk_per_trade,
            )
            return

        order_ref = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        entry_order_type = self._settings.entry_order_type.value

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=instrument["trading_symbol"],
            groww_symbol=instrument["groww_symbol"],
            transaction_type="BUY",
            order_type=entry_order_type,
            quantity=quantity,
            price=signal.entry_price,
            order_ref=order_ref,
            is_paper=True,
        )

        # Paper mode: immediately "fill" at the signal price
        await self._db.update_order_status(
            order_id=order_id,
            status="EXECUTED",
            filled_qty=quantity,
            avg_fill_price=signal.entry_price,
        )

        # Open a position
        await self._db.open_position(
            signal_id=signal_id,
            underlying=signal.underlying,
            trading_symbol=instrument["trading_symbol"],
            groww_symbol=instrument["groww_symbol"],
            quantity=quantity,
            avg_entry_price=signal.entry_price,
            option_type=signal.option_type.value,
            strike_price=signal.strike_price,
            expiry_day=signal.expiry_day,
            expiry_month=signal.expiry_month,
            stoploss=stoploss,
            targets=signal.targets,
            is_paper=True,
        )

        logger.info(
            "%s BUY %s x%d @ ₹%.2f | SL=₹%.2f Targets=%s",
            TAG_PAPER, signal.display_name, quantity, signal.entry_price,
            stoploss, signal.targets,
        )

    # ── Exit ─────────────────────────────────────────────────────────────

    async def execute_exit(self, signal: ExitSignal, signal_id: int) -> None:
        try:
            await self._execute_exit_inner(signal, signal_id)
        except Exception:
            logger.exception("%s Failed to execute exit for %s", TAG_PAPER, signal.display_name)
        finally:
            logger.info("")

    async def _execute_exit_inner(self, signal: ExitSignal, signal_id: int) -> None:
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
            if positions:
                position = positions[0]

        order_ref = f"PAPER-{uuid.uuid4().hex[:12].upper()}"

        if position is None:
            # No matching position in DB — still record the exit signal/order
            # for audit trail (the real platform may have the position).
            exit_price = signal.exit_price or 0.0
            trading_symbol = f"{signal.underlying}{signal.expiry_day or 0:02d}{(signal.expiry_month or 'XXX')[:3].upper()}{int(signal.strike_price or 0)}{signal.option_type.value if signal.option_type else 'XX'}"

            logger.warning(
                "%s No open position in DB for EXIT %s — "
                "recording exit order for audit trail",
                TAG_PAPER, signal.display_name,
            )

            order_id = await self._db.insert_order(
                signal_id=signal_id,
                trading_symbol=trading_symbol,
                transaction_type="SELL",
                order_type="LIMIT" if signal.exit_price is not None else "MARKET",
                quantity=0,
                price=exit_price,
                order_ref=order_ref,
                is_paper=True,
            )
            await self._db.update_order_status(
                order_id=order_id, status="EXECUTED",
            )
            return

        if signal.exit_price is not None:
            exit_price = signal.exit_price
        else:
            exit_price = position["avg_entry_price"]

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=position["trading_symbol"],
            groww_symbol=position.get("groww_symbol"),
            transaction_type="SELL",
            order_type="LIMIT" if signal.exit_price is not None else "MARKET",
            quantity=position["quantity"],
            price=exit_price,
            order_ref=order_ref,
            is_paper=True,
        )

        await self._db.update_order_status(
            order_id=order_id,
            status="EXECUTED",
            filled_qty=position["quantity"],
            avg_fill_price=exit_price,
        )

        pnl = (exit_price - position["avg_entry_price"]) * position["quantity"]
        await self._db.close_position(position["id"], pnl=pnl)

        logger.info(
            "%s EXIT %s x%d @ ₹%.2f | P&L: ₹%.2f",
            TAG_PAPER, position["trading_symbol"], position["quantity"],
            exit_price, pnl,
        )

    # ── Book Profit ──────────────────────────────────────────────────────

    async def execute_book_profit(self, signal: BookProfitSignal, signal_id: int) -> None:
        try:
            await self._execute_book_profit_inner(signal, signal_id)
        except Exception:
            logger.exception("%s Failed to execute book profit for %s", TAG_PAPER, signal.display_name)
        finally:
            logger.info("")

    async def _execute_book_profit_inner(self, signal: BookProfitSignal, signal_id: int) -> None:
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
            if positions:
                position = positions[0]

        if position is None:
            exit_price = signal.exit_price or 0.0
            trading_symbol = f"{signal.underlying}{signal.expiry_day or 0:02d}{(signal.expiry_month or 'XXX')[:3].upper()}{int(signal.strike_price or 0)}{signal.option_type.value if signal.option_type else 'XX'}"

            logger.warning(
                "%s No open position in DB for BOOK_PROFIT %s — "
                "recording order for audit trail",
                TAG_PAPER, signal.display_name,
            )

            order_ref = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
            order_id = await self._db.insert_order(
                signal_id=signal_id,
                trading_symbol=trading_symbol,
                transaction_type="SELL",
                order_type="LIMIT",
                quantity=0,
                price=exit_price,
                order_ref=order_ref,
                is_paper=True,
            )
            await self._db.update_order_status(
                order_id=order_id, status="EXECUTED",
            )
            return

        exit_price = signal.exit_price or position["avg_entry_price"]
        quantity = int(position["quantity"])
        entry_price = float(position["avg_entry_price"])

        if signal.is_partial:
            instrument = self.resolve_trading_symbol(
                position["underlying"],
                position.get("expiry_day") or 1,
                position.get("expiry_month") or "JAN",
                position.get("strike_price") or 0,
                position.get("option_type") or "CE",
            )
            lot_size = instrument.get("lot_size", 1) if instrument else 1

            if quantity <= lot_size:
                close_qty = quantity
            else:
                close_qty = quantity // 2
                if lot_size > 1 and close_qty >= lot_size:
                    close_qty = (close_qty // lot_size) * lot_size
                if close_qty <= 0:
                    close_qty = quantity
        else:
            close_qty = quantity

        remaining_qty = quantity - close_qty
        order_ref = f"PAPER-{uuid.uuid4().hex[:12].upper()}"

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=position["trading_symbol"],
            groww_symbol=position.get("groww_symbol"),
            transaction_type="SELL",
            order_type="LIMIT",
            quantity=close_qty,
            price=exit_price,
            order_ref=order_ref,
            is_paper=True,
        )

        await self._db.update_order_status(
            order_id=order_id,
            status="EXECUTED",
            filled_qty=close_qty,
            avg_fill_price=exit_price,
        )

        pnl = (exit_price - entry_price) * close_qty

        if remaining_qty > 0:
            await self._db.partial_close_position(
                position["id"],
                close_qty=close_qty,
                partial_pnl=pnl,
            )
            logger.info(
                "%s PARTIAL_BP %s | closed %d/%d @ ₹%.2f | P&L: ₹%.2f | remaining=%d",
                TAG_PAPER, position["trading_symbol"], close_qty, quantity,
                exit_price, pnl, remaining_qty,
            )
        else:
            await self._db.close_position(position["id"], pnl=pnl)
            logger.info(
                "%s BOOK_PROFIT %s x%d @ ₹%.2f | P&L: ₹%.2f",
                TAG_PAPER, position["trading_symbol"], close_qty, exit_price, pnl,
            )
