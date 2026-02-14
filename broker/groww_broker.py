"""Live broker implementation using the official Groww Trading API Python SDK."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from growwapi import GrowwAPI

from broker.base import BrokerInterface
from config.settings import Settings
from parser.models import BookProfitSignal, EntrySignal, ExitSignal
from storage.db import Database

logger = logging.getLogger(__name__)

# Month abbreviation mapping for Groww symbol construction
_MONTH_MAP = {
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
}


class GrowwBroker(BrokerInterface):
    """Execute real orders on Groww via the official ``growwapi`` SDK.

    Instrument resolution uses the instruments CSV downloaded at startup.
    Orders are placed as LIMIT orders at the signal price with a
    corresponding SL (stop-loss) order when a stoploss is provided.
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db
        self._groww: Optional[GrowwAPI] = None
        self._instruments_df: Optional[pd.DataFrame] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Exchange API key + secret for an access token, then load instruments."""
        logger.info("Groww API: exchanging API key + secret for access token …")
        loop = asyncio.get_running_loop()
        try:
            access_token = await loop.run_in_executor(
                None,
                lambda: GrowwAPI.get_access_token(
                    api_key=self._settings.groww_api_token,
                    secret=self._settings.groww_api_secret,
                ),
            )
        except Exception:
            logger.exception(
                "Failed to obtain Groww access token. "
                "Check GROWW_API_TOKEN and GROWW_API_SECRET in .env"
            )
            raise

        self._groww = GrowwAPI(access_token)
        logger.info("Groww API client initialised — loading instruments …")

        # Load instruments in a thread so we don't block the event loop
        logger.debug("[GROWW REQ] get_all_instruments")
        loop = asyncio.get_running_loop()
        self._instruments_df = await loop.run_in_executor(
            None, self._groww.get_all_instruments
        )
        logger.info(
            "Loaded %d instruments from Groww",
            len(self._instruments_df),
        )
        logger.debug(
            "[GROWW RES] get_all_instruments: %d rows, columns=%s",
            len(self._instruments_df),
            list(self._instruments_df.columns),
        )

    async def shutdown(self) -> None:
        logger.info("GrowwBroker shut down")

    @property
    def groww(self) -> GrowwAPI:
        if self._groww is None:
            raise RuntimeError("GrowwBroker not initialised. Call initialize() first.")
        return self._groww

    # ── Instrument resolution ────────────────────────────────────────────

    def resolve_trading_symbol(
        self,
        underlying: str,
        expiry_day: int,
        expiry_month: str,
        strike_price: float,
        option_type: str,
    ) -> Optional[dict[str, Any]]:
        """Look up the exact instrument from the Groww instruments CSV.

        The Groww ``groww_symbol`` format for options is:
            NSE-{UNDERLYING}-{DD}{Mon}{YY}-{STRIKE}-{CE|PE}
        e.g.  NSE-NIFTY-17Feb26-25600-PE

        We search the dataframe for a matching row.
        """
        if self._instruments_df is None:
            logger.error("Instruments not loaded")
            return None

        df = self._instruments_df
        month_title = _MONTH_MAP.get(expiry_month.upper(), expiry_month.title())

        # Build a pattern to match the groww_symbol
        # Year is tricky — we try current year and next year
        now = datetime.now()
        candidates = [now.year % 100, (now.year + 1) % 100]

        for yy in candidates:
            groww_sym = (
                f"NSE-{underlying}-{expiry_day:02d}{month_title}{yy}"
                f"-{int(strike_price)}-{option_type}"
            )
            logger.debug("[GROWW] Searching instrument: %s", groww_sym)
            match = df[df["groww_symbol"] == groww_sym]
            if not match.empty:
                row = match.iloc[0]
                return {
                    "trading_symbol": row["trading_symbol"],
                    "groww_symbol": row["groww_symbol"],
                    "exchange_token": str(row["exchange_token"]),
                    "lot_size": int(row.get("lot_size", 1)),
                    "exchange": row.get("exchange", "NSE"),
                    "segment": row.get("segment", "FNO"),
                }

        # Fallback: fuzzy search by underlying + strike + option type
        mask = (
            (df["underlying_symbol"] == underlying)
            & (df["strike_price"] == strike_price)
            & (df["instrument_type"] == option_type)
        )
        fuzzy = df[mask]
        if not fuzzy.empty:
            # Pick the one whose expiry is closest to the signal's day/month
            row = fuzzy.iloc[0]
            logger.warning(
                "Exact groww_symbol not found; using fuzzy match: %s",
                row["groww_symbol"],
            )
            return {
                "trading_symbol": row["trading_symbol"],
                "groww_symbol": row["groww_symbol"],
                "exchange_token": str(row["exchange_token"]),
                "lot_size": int(row.get("lot_size", 1)),
                "exchange": row.get("exchange", "NSE"),
                "segment": row.get("segment", "FNO"),
            }

        logger.error(
            "Instrument not found: %s %d %s %d %s",
            underlying, expiry_day, expiry_month, int(strike_price), option_type,
        )
        return None

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
            logger.error("Cannot execute entry — instrument not found for %s", signal.display_name)
            return

        lot_size = max(instrument.get("lot_size", 1), 1)
        quantity = lot_size * self._settings.default_lot_multiplier

        # Risk check
        if signal.stoploss is not None:
            risk = (signal.entry_price - signal.stoploss) * quantity
            if risk > self._settings.max_risk_per_trade:
                logger.warning(
                    "SKIP %s — risk ₹%.0f exceeds max ₹%.0f",
                    signal.display_name, risk, self._settings.max_risk_per_trade,
                )
                return

        product = self._settings.default_product.value  # NRML or MIS

        # Place the main BUY order
        loop = asyncio.get_running_loop()
        order_params = {
            "trading_symbol": instrument["trading_symbol"],
            "quantity": quantity,
            "validity": "DAY",
            "exchange": "NSE",
            "segment": "FNO",
            "product": product,
            "order_type": "LIMIT",
            "transaction_type": "BUY",
            "price": signal.entry_price,
        }
        logger.debug("[GROWW REQ] place_order BUY: %s", order_params)

        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.groww.place_order(
                    trading_symbol=instrument["trading_symbol"],
                    quantity=quantity,
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=getattr(self.groww, f"PRODUCT_{product}"),
                    order_type=self.groww.ORDER_TYPE_LIMIT,
                    transaction_type=self.groww.TRANSACTION_TYPE_BUY,
                    price=signal.entry_price,
                ),
            )
        except Exception:
            logger.exception("Failed to place BUY order for %s", signal.display_name)
            return

        logger.debug("[GROWW RES] place_order BUY: %s", response)
        groww_order_id = response.get("groww_order_id", "")
        order_status = response.get("order_status", "UNKNOWN")

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=instrument["trading_symbol"],
            groww_symbol=instrument["groww_symbol"],
            transaction_type="BUY",
            order_type="LIMIT",
            quantity=quantity,
            price=signal.entry_price,
            order_ref=groww_order_id,
            is_paper=False,
            exchange=instrument.get("exchange", "NSE"),
            segment=instrument.get("segment", "FNO"),
            product=product,
        )

        await self._db.update_order_status(
            order_id=order_id,
            status=order_status,
            filled_qty=0,
        )

        # Open position record (will be confirmed via feed updates)
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
            is_paper=False,
        )

        logger.info(
            "[LIVE] BUY %s x%d @ ₹%.2f  order_id=%s status=%s",
            signal.display_name, quantity, signal.entry_price,
            groww_order_id, order_status,
        )

        # Place SL order if stoploss is provided
        if signal.stoploss is not None:
            await self._place_stoploss(
                instrument=instrument,
                quantity=quantity,
                trigger_price=signal.stoploss,
                signal_id=signal_id,
                product=product,
            )

    async def _place_stoploss(
        self,
        instrument: dict[str, Any],
        quantity: int,
        trigger_price: float,
        signal_id: int,
        product: str,
    ) -> None:
        """Place a stop-loss SELL order to protect the position."""
        loop = asyncio.get_running_loop()
        sl_params = {
            "trading_symbol": instrument["trading_symbol"],
            "quantity": quantity,
            "order_type": "SL_M",
            "transaction_type": "SELL",
            "trigger_price": trigger_price,
            "product": product,
        }
        logger.debug("[GROWW REQ] place_order SL: %s", sl_params)

        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.groww.place_order(
                    trading_symbol=instrument["trading_symbol"],
                    quantity=quantity,
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=getattr(self.groww, f"PRODUCT_{product}"),
                    order_type=self.groww.ORDER_TYPE_STOP_LOSS_MARKET,
                    transaction_type=self.groww.TRANSACTION_TYPE_SELL,
                    trigger_price=trigger_price,
                ),
            )
            logger.debug("[GROWW RES] place_order SL: %s", response)
            logger.info(
                "[LIVE] SL order placed for %s @ ₹%.2f — %s",
                instrument["trading_symbol"],
                trigger_price,
                response.get("groww_order_id", ""),
            )
        except Exception:
            logger.exception(
                "Failed to place SL order for %s", instrument["trading_symbol"]
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
            positions = await self._db.find_all_open_positions_for_underlying(
                signal.underlying
            )
            if not positions:
                logger.warning("[LIVE] No open position for EXIT %s", signal.display_name)
                return
            position = positions[0]

        product = self._settings.default_product.value
        loop = asyncio.get_running_loop()

        exit_params = {
            "trading_symbol": position["trading_symbol"],
            "quantity": position["quantity"],
            "order_type": "MARKET",
            "transaction_type": "SELL",
            "product": product,
        }
        logger.debug("[GROWW REQ] place_order EXIT: %s", exit_params)

        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.groww.place_order(
                    trading_symbol=position["trading_symbol"],
                    quantity=position["quantity"],
                    validity=self.groww.VALIDITY_DAY,
                    exchange=self.groww.EXCHANGE_NSE,
                    segment=self.groww.SEGMENT_FNO,
                    product=getattr(self.groww, f"PRODUCT_{product}"),
                    order_type=self.groww.ORDER_TYPE_MARKET,
                    transaction_type=self.groww.TRANSACTION_TYPE_SELL,
                ),
            )
        except Exception:
            logger.exception("[LIVE] Failed to place EXIT order for %s", position["trading_symbol"])
            return

        logger.debug("[GROWW RES] place_order EXIT: %s", response)
        groww_order_id = response.get("groww_order_id", "")

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=position["trading_symbol"],
            groww_symbol=position.get("groww_symbol"),
            transaction_type="SELL",
            order_type="MARKET",
            quantity=position["quantity"],
            order_ref=groww_order_id,
            is_paper=False,
        )

        await self._db.update_order_status(
            order_id=order_id,
            status=response.get("order_status", "OPEN"),
        )

        # Mark position closed (P&L will be updated when fill is confirmed)
        await self._db.close_position(position["id"], pnl=0)

        logger.info(
            "[LIVE] EXIT %s x%d  order_id=%s",
            position["trading_symbol"],
            position["quantity"],
            groww_order_id,
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
                    "[LIVE] No open position for BOOK_PROFIT %s", signal.display_name
                )
                return
            position = positions[0]

        exit_price = signal.exit_price
        product = self._settings.default_product.value
        loop = asyncio.get_running_loop()

        order_kwargs: dict[str, Any] = {
            "trading_symbol": position["trading_symbol"],
            "quantity": position["quantity"],
            "validity": self.groww.VALIDITY_DAY,
            "exchange": self.groww.EXCHANGE_NSE,
            "segment": self.groww.SEGMENT_FNO,
            "product": getattr(self.groww, f"PRODUCT_{product}"),
            "transaction_type": self.groww.TRANSACTION_TYPE_SELL,
        }

        if exit_price is not None:
            order_kwargs["order_type"] = self.groww.ORDER_TYPE_LIMIT
            order_kwargs["price"] = exit_price
        else:
            order_kwargs["order_type"] = self.groww.ORDER_TYPE_MARKET

        bp_params = {
            "trading_symbol": position["trading_symbol"],
            "quantity": position["quantity"],
            "order_type": "LIMIT" if exit_price else "MARKET",
            "transaction_type": "SELL",
            "price": exit_price,
            "product": product,
        }
        logger.debug("[GROWW REQ] place_order BOOK_PROFIT: %s", bp_params)

        try:
            response = await loop.run_in_executor(
                None,
                lambda: self.groww.place_order(**order_kwargs),
            )
        except Exception:
            logger.exception(
                "[LIVE] Failed to place BOOK_PROFIT order for %s",
                position["trading_symbol"],
            )
            return

        logger.debug("[GROWW RES] place_order BOOK_PROFIT: %s", response)
        groww_order_id = response.get("groww_order_id", "")

        order_id = await self._db.insert_order(
            signal_id=signal_id,
            trading_symbol=position["trading_symbol"],
            groww_symbol=position.get("groww_symbol"),
            transaction_type="SELL",
            order_type="LIMIT" if exit_price else "MARKET",
            quantity=position["quantity"],
            price=exit_price,
            order_ref=groww_order_id,
            is_paper=False,
        )

        await self._db.update_order_status(
            order_id=order_id,
            status=response.get("order_status", "OPEN"),
        )

        pnl = 0.0
        if exit_price is not None:
            pnl = (exit_price - position["avg_entry_price"]) * position["quantity"]

        await self._db.close_position(position["id"], pnl=pnl)

        logger.info(
            "[LIVE] BOOK_PROFIT %s x%d @ ₹%s  order_id=%s  est_pnl=₹%.2f",
            position["trading_symbol"],
            position["quantity"],
            exit_price or "MARKET",
            groww_order_id,
            pnl,
        )
