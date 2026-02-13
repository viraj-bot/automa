"""Data models for parsed trade signals."""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class SignalType(str, enum.Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    BOOK_PROFIT = "BOOK_PROFIT"


class OptionType(str, enum.Enum):
    CE = "CE"
    PE = "PE"


class TransactionSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


# ── Mapping of common aliases to canonical underlying symbols ──
UNDERLYING_ALIASES: dict[str, str] = {
    "NIFTY50": "NIFTY",
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "BANK NIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "FIN NIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}


def _normalize_underlying(raw: str) -> str:
    """Normalize an underlying symbol to its canonical form.

    If the symbol is not in the alias map it is returned upper-cased as-is
    (covers stock names like INDHOTEL, RELIANCE, etc.).
    """
    key = raw.strip().upper().replace("-", "").replace("_", "")
    return UNDERLYING_ALIASES.get(key, key)


@dataclass(frozen=True)
class EntrySignal:
    """A new trade entry signal parsed from a Telegram message."""

    signal_type: SignalType = field(default=SignalType.ENTRY, init=False)
    underlying: str  # canonical symbol, e.g. "NIFTY"
    expiry_day: int  # day of month
    expiry_month: str  # 3-letter month, e.g. "FEB"
    strike_price: float
    option_type: OptionType  # CE or PE
    entry_price: float
    targets: list[float] = field(default_factory=list)
    stoploss: Optional[float] = None
    raw_text: str = ""
    message_id: Optional[int] = None
    timestamp: Optional[datetime] = None

    @property
    def signal_hash(self) -> str:
        """Deterministic hash for idempotency."""
        key = f"{self.underlying}:{self.expiry_day}{self.expiry_month}:{self.strike_price}:{self.option_type.value}:{self.entry_price}"
        if self.message_id is not None:
            key += f":{self.message_id}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def display_name(self) -> str:
        return (
            f"{self.underlying} {self.expiry_day} {self.expiry_month} "
            f"{int(self.strike_price)} {self.option_type.value}"
        )


@dataclass(frozen=True)
class ExitSignal:
    """An exit signal — close an existing position."""

    signal_type: SignalType = field(default=SignalType.EXIT, init=False)
    underlying: str
    expiry_day: Optional[int] = None
    expiry_month: Optional[str] = None
    strike_price: Optional[float] = None
    option_type: Optional[OptionType] = None
    raw_text: str = ""
    message_id: Optional[int] = None
    timestamp: Optional[datetime] = None

    @property
    def signal_hash(self) -> str:
        key = f"EXIT:{self.underlying}:{self.expiry_day}{self.expiry_month}:{self.strike_price}:{self.option_type}"
        if self.message_id is not None:
            key += f":{self.message_id}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def display_name(self) -> str:
        parts = [self.underlying]
        if self.expiry_day and self.expiry_month:
            parts.append(f"{self.expiry_day} {self.expiry_month}")
        if self.strike_price:
            parts.append(str(int(self.strike_price)))
        if self.option_type:
            parts.append(self.option_type.value)
        return " ".join(parts)


@dataclass(frozen=True)
class BookProfitSignal:
    """A book-profit signal — partially or fully exit at a given price."""

    signal_type: SignalType = field(default=SignalType.BOOK_PROFIT, init=False)
    underlying: str
    expiry_day: Optional[int] = None
    expiry_month: Optional[str] = None
    strike_price: Optional[float] = None
    option_type: Optional[OptionType] = None
    exit_price: Optional[float] = None
    raw_text: str = ""
    message_id: Optional[int] = None
    timestamp: Optional[datetime] = None

    @property
    def signal_hash(self) -> str:
        key = f"BP:{self.underlying}:{self.expiry_day}{self.expiry_month}:{self.strike_price}:{self.option_type}:{self.exit_price}"
        if self.message_id is not None:
            key += f":{self.message_id}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def display_name(self) -> str:
        parts = [self.underlying]
        if self.expiry_day and self.expiry_month:
            parts.append(f"{self.expiry_day} {self.expiry_month}")
        if self.strike_price:
            parts.append(str(int(self.strike_price)))
        if self.option_type:
            parts.append(self.option_type.value)
        return " ".join(parts)


# Union type for convenience
TradeSignal = EntrySignal | ExitSignal | BookProfitSignal
