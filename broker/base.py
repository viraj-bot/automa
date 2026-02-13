"""Abstract broker interface that both live and paper brokers implement."""

from __future__ import annotations

import abc
from typing import Any, Optional

from parser.models import BookProfitSignal, EntrySignal, ExitSignal, TradeSignal


class BrokerInterface(abc.ABC):
    """Contract that every broker implementation must satisfy."""

    # ── Lifecycle ────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def initialize(self) -> None:
        """Perform any startup work (load instruments, connect feeds, etc.)."""

    @abc.abstractmethod
    async def shutdown(self) -> None:
        """Graceful teardown."""

    # ── Instrument resolution ────────────────────────────────────────────

    @abc.abstractmethod
    def resolve_trading_symbol(
        self,
        underlying: str,
        expiry_day: int,
        expiry_month: str,
        strike_price: float,
        option_type: str,
    ) -> Optional[dict[str, Any]]:
        """Map parsed signal fields to a broker-specific instrument.

        Returns a dict with at least ``trading_symbol`` and ``groww_symbol``
        keys, or ``None`` if the instrument cannot be found.
        """

    # ── Order execution ──────────────────────────────────────────────────

    @abc.abstractmethod
    async def execute_entry(self, signal: EntrySignal, signal_id: int) -> None:
        """Place a BUY order for a new entry signal."""

    @abc.abstractmethod
    async def execute_exit(self, signal: ExitSignal, signal_id: int) -> None:
        """Close an open position matching the exit signal."""

    @abc.abstractmethod
    async def execute_book_profit(self, signal: BookProfitSignal, signal_id: int) -> None:
        """Book profit on a matching open position."""

    # ── Convenience dispatcher ───────────────────────────────────────────

    async def execute(self, signal: TradeSignal, signal_id: int) -> None:
        """Route a signal to the appropriate handler."""
        if isinstance(signal, EntrySignal):
            await self.execute_entry(signal, signal_id)
        elif isinstance(signal, ExitSignal):
            await self.execute_exit(signal, signal_id)
        elif isinstance(signal, BookProfitSignal):
            await self.execute_book_profit(signal, signal_id)
        else:
            raise ValueError(f"Unknown signal type: {type(signal)}")
