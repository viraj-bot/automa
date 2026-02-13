"""Paper-trading broker that simulates order execution without real money."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from broker.base import BrokerInterface
from config.settings import Settings
from parser.models import BookProfitSignal, EntrySignal, ExitSignal
from storage.db import Database

logger = logging.getLogger(__name__)


class PaperBroker(BrokerInterface):
    """Simulates trades in SQLite.  No real money is involved."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        logger.info("PaperBroker initialised (no real orders will be placed)")

    async def shutdown(self) -> None:
        logger.info("PaperBroker shut down")

    # ── Instrument resolution (paper mode: passthrough) ──────────────────

    def resolve_trading_symbol(
        self,
        underlying: str,
        expiry_day: int,
        expiry_month: str,
        strike_price: float,
        option_type: str,
    ) -> Optional[dict[str, Any]]:
        """Build a synthetic trading symbol for paper trades.

        In paper mode we don't need to look up the real instruments CSV;
        we construct a symbol that mirrors the Groww convention so that
        the rest of the pipeline stays consistent.
        """
        # e.g. NIFTY25FEB25600PE
        month_short = expiry_month[:3].upper()
        ts = f"{underlying}{expiry_day:02d}{month_short}{int(strike_price)}{option_type}"
        gs = f"NSE-{underlying}-{expiry_day:02d}{month_short}-{int(strike_price)}-{option_type}"
        return {
            "trading_symbol": ts,
            "groww_symbol": gs,
            "lot_size": 1,  # will be overridden by real broker
        }

    # ── Entry ────────────────────────────────────────────────────────────

    async def execute_entry(self, signal: EntrySignal, signal_id: int) -> None:
        instrument = self.resolve_trading_symbol(
            signal.underlying,
            signal.expiry_day,
            signal.expiry_month,
            signal.strike_price,
            signal.option_type.value,
        )
        if instrument is None:
            logger.warning("Could not resolve instrument for %s", signal.display_name)
            return

        lot_size = max(instrument.get("lot_size", 1), 1)
        quantity = lot_size * self._settings.default_lot_multiplier

        # Risk check
        if signal.stoploss is not None:
            risk = (signal.entry_price - signal.stoploss) * quantity
            if risk > self._settings.max_risk_per_trade:
                logger.warning(
                    "SKIP %s — risk ₹%.0f exceeds max ₹%.0f",
                    signal.display_name,
                    risk,
                    self._settings.max_risk_per_trade,
                )
                return

        order_ref = f"PAPER-{uuid.uuid4().hex[:12].upper()}"

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=instrument["trading_symbol"],
            groww_symbol=instrument["groww_symbol"],
            transaction_type="BUY",
            order_type="LIMIT",
            quantity=quantity,
            price=signal.entry_price,
            order_ref=order_ref,
            is_paper=True,
        )

        # Immediately "fill" the paper order
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
            stoploss=signal.stoploss,
            targets=signal.targets,
            is_paper=True,
        )

        logger.info(
            "[PAPER] BUY %s x%d @ ₹%.2f  (SL: %s, Targets: %s)",
            signal.display_name,
            quantity,
            signal.entry_price,
            signal.stoploss,
            signal.targets,
        )

    # ── Exit ─────────────────────────────────────────────────────────────

    async def execute_exit(self, signal: ExitSignal, signal_id: int) -> None:
        position = await self._db.find_open_position(
            underlying=signal.underlying,
            strike_price=signal.strike_price,
            option_type=signal.option_type.value if signal.option_type else None,
            expiry_day=signal.expiry_day,
            expiry_month=signal.expiry_month,
        )

        if position is None:
            # Try broader match — just underlying
            positions = await self._db.find_all_open_positions_for_underlying(
                signal.underlying
            )
            if not positions:
                logger.warning(
                    "[PAPER] No open position found for EXIT %s", signal.display_name
                )
                return
            position = positions[0]  # close the most recent

        order_ref = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        exit_price = position["avg_entry_price"]  # paper: exit at entry (conservative)

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=position["trading_symbol"],
            groww_symbol=position.get("groww_symbol"),
            transaction_type="SELL",
            order_type="MARKET",
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
            "[PAPER] EXIT %s x%d @ ₹%.2f  P&L: ₹%.2f",
            position["trading_symbol"],
            position["quantity"],
            exit_price,
            pnl,
        )

    # ── Book Profit ──────────────────────────────────────────────────────

    async def execute_book_profit(self, signal: BookProfitSignal, signal_id: int) -> None:
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
                logger.warning(
                    "[PAPER] No open position found for BOOK_PROFIT %s",
                    signal.display_name,
                )
                return
            position = positions[0]

        exit_price = signal.exit_price or position["avg_entry_price"]
        order_ref = f"PAPER-{uuid.uuid4().hex[:12].upper()}"

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=position["trading_symbol"],
            groww_symbol=position.get("groww_symbol"),
            transaction_type="SELL",
            order_type="LIMIT",
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
            "[PAPER] BOOK_PROFIT %s x%d @ ₹%.2f  P&L: ₹%.2f",
            position["trading_symbol"],
            position["quantity"],
            exit_price,
            pnl,
        )
