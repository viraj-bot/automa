"""Live broker implementation using the official Groww Trading API Python SDK."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIAuthenticationException

from broker.base import BrokerInterface
from config.settings import Settings
from parser.models import BookProfitSignal, EntrySignal, ExitSignal
from storage.db import Database

from config.log_config import (
    TAG_GROWW_REQ, TAG_GROWW_RES, TAG_LIVE, TAG_RISK, TAG_CRITICAL,
)

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
        """Obtain an access token (TOTP preferred, API-key/secret fallback), then load instruments."""
        loop = asyncio.get_running_loop()
        use_totp = bool(
            self._settings.groww_totp_token and self._settings.groww_totp_secret
        )

        try:
            if use_totp:
                import pyotp

                logger.info("%s Authenticating via TOTP (no daily approval needed) …", TAG_GROWW_REQ)
                totp_code = pyotp.TOTP(self._settings.groww_totp_secret).now()
                access_token = await loop.run_in_executor(
                    None,
                    lambda: GrowwAPI.get_access_token(
                        api_key=self._settings.groww_totp_token,
                        totp=totp_code,
                    ),
                )
            else:
                logger.info("%s Authenticating via API key + secret (requires daily approval) …", TAG_GROWW_REQ)
                access_token = await loop.run_in_executor(
                    None,
                    lambda: GrowwAPI.get_access_token(
                        api_key=self._settings.groww_api_token,
                        secret=self._settings.groww_api_secret,
                    ),
                )
        except Exception:
            method = "TOTP" if use_totp else "API key/secret"
            logger.exception(
                "Failed to obtain Groww access token via %s. "
                "Check your .env credentials.",
                method,
            )
            raise

        self._groww = GrowwAPI(access_token)
        logger.info("%s Authenticated — loading instruments …", TAG_GROWW_RES)

        logger.info("%s get_all_instruments", TAG_GROWW_REQ)
        loop = asyncio.get_running_loop()
        self._instruments_df = await loop.run_in_executor(
            None, self._groww.get_all_instruments
        )
        logger.info(
            "%s Loaded %d instruments (%d columns)",
            TAG_GROWW_RES, len(self._instruments_df), len(self._instruments_df.columns),
        )

    async def shutdown(self) -> None:
        logger.info("%s GrowwBroker shut down", TAG_LIVE)

    @property
    def groww(self) -> GrowwAPI:
        if self._groww is None:
            raise RuntimeError("GrowwBroker not initialised. Call initialize() first.")
        return self._groww

    async def _reauthenticate(self) -> None:
        """Generate a fresh TOTP code and obtain a new access token."""
        loop = asyncio.get_running_loop()
        use_totp = bool(
            self._settings.groww_totp_token and self._settings.groww_totp_secret
        )
        try:
            if use_totp:
                import pyotp

                logger.info("%s Re-authenticating via TOTP (token expired) …", TAG_GROWW_REQ)
                totp_code = pyotp.TOTP(self._settings.groww_totp_secret).now()
                access_token = await loop.run_in_executor(
                    None,
                    lambda: GrowwAPI.get_access_token(
                        api_key=self._settings.groww_totp_token,
                        totp=totp_code,
                    ),
                )
            else:
                logger.info("%s Re-authenticating via API key + secret (token expired) …", TAG_GROWW_REQ)
                access_token = await loop.run_in_executor(
                    None,
                    lambda: GrowwAPI.get_access_token(
                        api_key=self._settings.groww_api_token,
                        secret=self._settings.groww_api_secret,
                    ),
                )
            self._groww = GrowwAPI(access_token)
            logger.info("%s Re-authenticated successfully", TAG_GROWW_RES)
        except Exception:
            logger.exception("%s Re-authentication failed", TAG_CRITICAL)
            raise

    async def _call_groww_api(self, func):
        """Call a zero-arg callable that invokes the Groww SDK.

        On ``GrowwAPIAuthenticationException`` the broker re-authenticates
        once and retries.  Because *func* is typically a lambda that
        references ``self.groww``, the retry naturally picks up the
        refreshed client instance.
        """
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, func)
        except GrowwAPIAuthenticationException:
            logger.warning("%s Access token expired — re-authenticating …", TAG_GROWW_REQ)
            await self._reauthenticate()
            return await loop.run_in_executor(None, func)

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
        try:
            await self._execute_entry_inner(signal, signal_id)
        except Exception:
            logger.exception("%s Unhandled error in execute_entry for %s", TAG_LIVE, signal.display_name)

    async def _execute_entry_inner(self, signal: EntrySignal, signal_id: int) -> None:
        instrument = self.resolve_trading_symbol(
            signal.underlying,
            signal.expiry_day,
            signal.expiry_month,
            signal.strike_price,
            signal.option_type.value,
        )
        if instrument is None:
            logger.error("%s Cannot execute entry — instrument not found for %s", TAG_LIVE, signal.display_name)
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

        product = self._settings.default_product.value  # NRML or MIS
        use_market = self._settings.entry_order_type.value == "MARKET"

        order_kwargs: dict[str, Any] = {
            "trading_symbol": instrument["trading_symbol"],
            "quantity": quantity,
            "validity": self.groww.VALIDITY_DAY,
            "exchange": self.groww.EXCHANGE_NSE,
            "segment": self.groww.SEGMENT_FNO,
            "product": getattr(self.groww, f"PRODUCT_{product}"),
            "transaction_type": self.groww.TRANSACTION_TYPE_BUY,
        }

        if use_market:
            order_kwargs["order_type"] = self.groww.ORDER_TYPE_MARKET
            order_type_str = "MARKET"
        else:
            order_kwargs["order_type"] = self.groww.ORDER_TYPE_LIMIT
            order_kwargs["price"] = signal.entry_price
            order_type_str = "LIMIT"

        order_params = {
            "trading_symbol": instrument["trading_symbol"],
            "quantity": quantity,
            "order_type": order_type_str,
            "transaction_type": "BUY",
            "price": signal.entry_price if not use_market else "MARKET",
            "product": product,
            "stoploss": stoploss,
            "targets": signal.targets,
        }
        logger.info("%s place_order BUY %s", TAG_GROWW_REQ, order_params)

        try:
            response = await self._call_groww_api(
                lambda: self.groww.place_order(**order_kwargs),
            )
        except Exception:
            logger.exception("%s Failed to place BUY order for %s", TAG_LIVE, signal.display_name)
            return

        groww_order_id = response.get("groww_order_id", "")
        order_status = response.get("order_status", "UNKNOWN")
        logger.info("%s place_order BUY → %s", TAG_GROWW_RES, response)
        logger.info(
            "%s BUY %s x%d @ ₹%.2f | order_id=%s status=%s | SL=₹%.2f Targets=%s",
            TAG_LIVE, signal.display_name, quantity, signal.entry_price,
            groww_order_id, order_status, stoploss, signal.targets,
        )

        # Record in DB — if this fails, the order is live on Groww but
        # unrecorded locally.  Log critically but don't crash.
        try:
            order_id = await self._db.insert_order(
                signal_id=signal_id,
                trading_symbol=instrument["trading_symbol"],
                groww_symbol=instrument["groww_symbol"],
                transaction_type="BUY",
                order_type=order_type_str,
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
                is_paper=False,
            )
        except Exception:
            logger.exception(
                "%s BUY order %s placed on Groww but DB recording failed! "
                "Manual reconciliation needed.",
                TAG_CRITICAL, groww_order_id,
            )

        await self._place_stoploss(
            instrument=instrument,
            quantity=quantity,
            trigger_price=stoploss,
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
        sl_params = {
            "trading_symbol": instrument["trading_symbol"],
            "quantity": quantity,
            "order_type": "SL_M",
            "transaction_type": "SELL",
            "trigger_price": trigger_price,
            "product": product,
        }
        logger.info("%s place_order SL %s", TAG_GROWW_REQ, sl_params)

        try:
            response = await self._call_groww_api(
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
            logger.info("%s place_order SL → %s", TAG_GROWW_RES, response)
            logger.info(
                "%s SL placed for %s @ ₹%.2f | order_id=%s",
                TAG_LIVE, instrument["trading_symbol"], trigger_price,
                response.get("groww_order_id", ""),
            )
        except Exception:
            logger.exception(
                "%s Failed to place SL order for %s", TAG_LIVE, instrument["trading_symbol"]
            )

    # ── Exit ─────────────────────────────────────────────────────────────

    async def execute_exit(self, signal: ExitSignal, signal_id: int) -> None:
        try:
            await self._execute_exit_inner(signal, signal_id)
        except Exception:
            logger.exception("%s Unhandled error in execute_exit for %s", TAG_LIVE, signal.display_name)

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
            if not positions:
                logger.warning("%s No open position for EXIT %s", TAG_LIVE, signal.display_name)
                return
            position = positions[0]

        product = self._settings.default_product.value
        use_market = self._settings.exit_order_type.value == "MARKET"

        exit_price = signal.exit_price
        if use_market or exit_price is None:
            order_type_str = "MARKET"
            order_type_const = self.groww.ORDER_TYPE_MARKET
        else:
            order_type_str = "LIMIT"
            order_type_const = self.groww.ORDER_TYPE_LIMIT

        order_kwargs: dict[str, Any] = {
            "trading_symbol": position["trading_symbol"],
            "quantity": position["quantity"],
            "validity": self.groww.VALIDITY_DAY,
            "exchange": self.groww.EXCHANGE_NSE,
            "segment": self.groww.SEGMENT_FNO,
            "product": getattr(self.groww, f"PRODUCT_{product}"),
            "order_type": order_type_const,
            "transaction_type": self.groww.TRANSACTION_TYPE_SELL,
        }
        if order_type_str == "LIMIT" and exit_price is not None:
            order_kwargs["price"] = exit_price

        exit_params = {
            "trading_symbol": position["trading_symbol"],
            "quantity": position["quantity"],
            "order_type": order_type_str,
            "transaction_type": "SELL",
            "price": exit_price if order_type_str == "LIMIT" else "MARKET",
            "product": product,
        }
        logger.info("%s place_order EXIT %s", TAG_GROWW_REQ, exit_params)

        try:
            response = await self._call_groww_api(
                lambda: self.groww.place_order(**order_kwargs),
            )
        except Exception:
            logger.exception("%s Failed to place EXIT order for %s", TAG_LIVE, position["trading_symbol"])
            return

        groww_order_id = response.get("groww_order_id", "")
        logger.info("%s place_order EXIT → %s", TAG_GROWW_RES, response)

        pnl = 0.0
        if exit_price is not None:
            pnl = (exit_price - position["avg_entry_price"]) * position["quantity"]

        logger.info(
            "%s EXIT %s x%d @ ₹%s | order_id=%s | P&L: ₹%.2f",
            TAG_LIVE, position["trading_symbol"], position["quantity"],
            exit_price or "MARKET", groww_order_id, pnl,
        )

        try:
            order_id = await self._db.insert_order(
                signal_id=signal_id,
                trading_symbol=position["trading_symbol"],
                groww_symbol=position.get("groww_symbol"),
                transaction_type="SELL",
                order_type=order_type_str,
                quantity=position["quantity"],
                price=exit_price,
                order_ref=groww_order_id,
                is_paper=False,
            )

            await self._db.update_order_status(
                order_id=order_id,
                status=response.get("order_status", "OPEN"),
            )

            await self._db.close_position(position["id"], pnl=pnl)
        except Exception:
            logger.exception(
                "%s EXIT order %s placed on Groww but DB recording failed! "
                "Manual reconciliation needed.",
                TAG_CRITICAL, groww_order_id,
            )

    # ── Book Profit ──────────────────────────────────────────────────────

    async def execute_book_profit(self, signal: BookProfitSignal, signal_id: int) -> None:
        try:
            await self._execute_book_profit_inner(signal, signal_id)
        except Exception:
            logger.exception("%s Unhandled error in execute_book_profit for %s", TAG_LIVE, signal.display_name)

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
            if not positions:
                logger.warning(
                    "%s No open position for BOOK_PROFIT %s", TAG_LIVE, signal.display_name
                )
                return
            position = positions[0]

        exit_price = signal.exit_price
        quantity = int(position["quantity"])
        entry_price = float(position["avg_entry_price"])
        product = self._settings.default_product.value
        use_market = self._settings.exit_order_type.value == "MARKET"

        # Determine how much to close.
        # For partial book-profit with 1 lot, close the entire position.
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

        # Determine order type for the book-profit exit
        if use_market or exit_price is None:
            order_type_str = "MARKET"
            order_type_const = self.groww.ORDER_TYPE_MARKET
        else:
            order_type_str = "LIMIT"
            order_type_const = self.groww.ORDER_TYPE_LIMIT

        order_kwargs: dict[str, Any] = {
            "trading_symbol": position["trading_symbol"],
            "quantity": close_qty,
            "validity": self.groww.VALIDITY_DAY,
            "exchange": self.groww.EXCHANGE_NSE,
            "segment": self.groww.SEGMENT_FNO,
            "product": getattr(self.groww, f"PRODUCT_{product}"),
            "transaction_type": self.groww.TRANSACTION_TYPE_SELL,
            "order_type": order_type_const,
        }
        if order_type_str == "LIMIT" and exit_price is not None:
            order_kwargs["price"] = exit_price

        bp_params = {
            "trading_symbol": position["trading_symbol"],
            "quantity": close_qty,
            "order_type": order_type_str,
            "transaction_type": "SELL",
            "price": exit_price if order_type_str == "LIMIT" else "MARKET",
            "product": product,
            "partial": signal.is_partial,
        }
        logger.info("%s place_order BOOK_PROFIT %s", TAG_GROWW_REQ, bp_params)

        try:
            response = await self._call_groww_api(
                lambda: self.groww.place_order(**order_kwargs),
            )
        except Exception:
            logger.exception(
                "%s Failed to place BOOK_PROFIT order for %s",
                TAG_LIVE, position["trading_symbol"],
            )
            return

        groww_order_id = response.get("groww_order_id", "")
        logger.info("%s place_order BOOK_PROFIT → %s", TAG_GROWW_RES, response)

        pnl = 0.0
        if exit_price is not None:
            pnl = (exit_price - entry_price) * close_qty

        try:
            order_id = await self._db.insert_order(
                signal_id=signal_id,
                trading_symbol=position["trading_symbol"],
                groww_symbol=position.get("groww_symbol"),
                transaction_type="SELL",
                order_type=order_type_str,
                quantity=close_qty,
                price=exit_price,
                order_ref=groww_order_id,
                is_paper=False,
            )

            await self._db.update_order_status(
                order_id=order_id,
                status=response.get("order_status", "OPEN"),
            )

            if remaining_qty > 0:
                await self._db.partial_close_position(
                    position["id"],
                    close_qty=close_qty,
                    partial_pnl=pnl,
                )
            else:
                await self._db.close_position(position["id"], pnl=pnl)
        except Exception:
            logger.exception(
                "%s BOOK_PROFIT order %s placed on Groww but DB recording "
                "failed! Manual reconciliation needed.",
                TAG_CRITICAL, groww_order_id,
            )

        if remaining_qty > 0:
            logger.info(
                "%s PARTIAL_BP %s | closed %d/%d @ ₹%s | order_id=%s | P&L: ₹%.2f | remaining=%d",
                TAG_LIVE, position["trading_symbol"], close_qty, quantity,
                exit_price or "MARKET", groww_order_id, pnl, remaining_qty,
            )
        else:
            logger.info(
                "%s BOOK_PROFIT %s x%d @ ₹%s | order_id=%s | P&L: ₹%.2f",
                TAG_LIVE, position["trading_symbol"], close_qty,
                exit_price or "MARKET", groww_order_id, pnl,
            )
