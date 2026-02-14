"""Regex-based parser that converts raw Telegram messages into structured trade signals.

Handles three signal types:
  1. ENTRY  – "Buy NIFTY50 17 FEB 25600 PE at 135"
  2. EXIT   – "Exit Nifty50 17 FEB 25600 PE"
  3. BOOK_PROFIT – "Book Profit in INDHOTEL 24 FEB 690 CE at price 17.2"

The parser is intentionally lenient: it strips emojis, normalises whitespace,
and tries multiple regex patterns so that slight wording variations still match.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from parser.models import (
    BookProfitSignal,
    EntrySignal,
    ExitSignal,
    OptionType,
    TradeSignal,
    _normalize_underlying,
)

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────────

_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map
    "\U0001f1e0-\U0001f1ff"  # flags
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\u200d\ufe0f"
    "]+",
    flags=re.UNICODE,
)

_MONTHS = {
    "JAN": "JAN", "FEB": "FEB", "MAR": "MAR", "APR": "APR",
    "MAY": "MAY", "JUN": "JUN", "JUL": "JUL", "AUG": "AUG",
    "SEP": "SEP", "OCT": "OCT", "NOV": "NOV", "DEC": "DEC",
    # full names
    "JANUARY": "JAN", "FEBRUARY": "FEB", "MARCH": "MAR",
    "APRIL": "APR", "JUNE": "JUN", "JULY": "JUL",
    "AUGUST": "AUG", "SEPTEMBER": "SEP", "OCTOBER": "OCT",
    "NOVEMBER": "NOV", "DECEMBER": "DEC",
}

_MONTH_PATTERN = "|".join(_MONTHS.keys())

# ── Instrument pattern (shared across signal types) ──
# Matches: NIFTY50 17 FEB 25600 PE  or  INDHOTEL 24 FEB 690 CE
# Groups:  underlying, day, month, strike, option_type
# The underlying group does NOT allow spaces — it captures a single token
# like NIFTY50, BANKNIFTY, INDHOTEL, etc.
_INSTRUMENT_RE = re.compile(
    r"(?P<underlying>[A-Z][A-Z0-9]{1,20})\s+"
    r"(?P<day>\d{1,2})\s+"
    rf"(?P<month>{_MONTH_PATTERN})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s*"
    r"(?P<otype>CE|PE)",
    re.IGNORECASE,
)

# ── Price extraction helpers ──
_RUPEE_PRICE_RE = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Matches individual "target N: <price>" patterns where N is a 1-2 digit number.
# The target *number* (1, 2, 3 …) is consumed by the non-capturing \d{1,2} so
# that the capturing group always gets the *price*.
_TARGET_LABELLED_RE = re.compile(
    r"(?:target|tgt|tp)\s*(\d{1,2})\s*[:=\-]\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Matches "targets: ₹60 / ₹90 / ₹120" or "tgt: 60, 90, 120" or
# "target: ₹185 / ₹235" style — a keyword (without a trailing digit)
# followed by a separator and a list of prices.
_TARGET_LIST_RE = re.compile(
    r"(?:targets?|tgt|tp)\s*[:=\-]\s*"
    r"((?:(?:₹|rs\.?|inr)?\s*\d+(?:\.\d+)?\s*[/,]\s*)*"
    r"(?:₹|rs\.?|inr)?\s*\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Extracts individual numeric values from a matched target-list string.
_PRICE_IN_LIST_RE = re.compile(r"(\d+(?:\.\d+)?)")

_STOPLOSS_RE = re.compile(
    r"(?:stoploss|stop\s*loss|sl)\s*[:=\-]?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _clean_text(text: str) -> str:
    """Remove emojis, collapse whitespace, strip."""
    text = _EMOJI_RE.sub(" ", text)
    text = re.sub(r"[^\S\n]+", " ", text)  # collapse horizontal whitespace
    return text.strip()


def _extract_price_after(text: str, keyword: str) -> Optional[float]:
    """Find a price value that appears after *keyword* in the text."""
    pattern = re.compile(
        rf"{re.escape(keyword)}\s*(?:at|@|:)?\s*(?:₹|rs\.?|inr|price)?\s*(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if m:
        return float(m.group(1))
    return None


def _extract_targets(text: str) -> list[float]:
    """Extract all target prices from the text.

    Handles two common formats:
      1. Labelled:  "target 1: 185  target 2: 235"
      2. List:      "targets: ₹60 / ₹90 / ₹120"  or  "tgt: 60, 90, 120"
    """
    # Strategy 1: labelled targets  (target 1: 185, target 2: 235, …)
    # group(1) = target number, group(2) = price
    labelled = [float(m.group(2)) for m in _TARGET_LABELLED_RE.finditer(text)]
    if labelled:
        return labelled

    # Strategy 2: single keyword followed by a comma/slash-separated price list
    list_match = _TARGET_LIST_RE.search(text)
    if list_match:
        raw_list = list_match.group(1)
        prices = [float(p.group(1)) for p in _PRICE_IN_LIST_RE.finditer(raw_list)]
        if prices:
            return prices

    return []


def _extract_stoploss(text: str) -> Optional[float]:
    m = _STOPLOSS_RE.search(text)
    return float(m.group(1)) if m else None


def _normalise_month(raw: str) -> str:
    return _MONTHS.get(raw.upper(), raw.upper()[:3])


# ── Signal-type keyword detectors ──

_ENTRY_KEYWORDS = re.compile(
    r"\b(buy|new\s+option\s+trade|fresh\s+entry|enter|long|go\s+long|initiate)\b",
    re.IGNORECASE,
)

_EXIT_KEYWORDS = re.compile(
    r"\b(exit|close|square\s*off|please\s+exit|exit\s+from|get\s+out)\b",
    re.IGNORECASE,
)

_BOOK_PROFIT_KEYWORDS = re.compile(
    r"\b(book\s+profit|partial\s+profit|trail\s+sl|book\s+partial|take\s+profit)\b",
    re.IGNORECASE,
)


# ── Public API ───────────────────────────────────────────────────────────────

class SignalParser:
    """Stateless parser that converts raw message text into a TradeSignal or None."""

    def parse(
        self,
        text: str,
        message_id: Optional[int] = None,
        timestamp: Optional[datetime] = None,
    ) -> Optional[TradeSignal]:
        """Attempt to parse *text* into a trade signal.

        Returns ``None`` if the message is not recognised as a trade signal.
        """
        if not text or len(text) < 5:
            return None

        cleaned = _clean_text(text)

        # Determine signal type by keyword priority:
        # BOOK_PROFIT > EXIT > ENTRY  (book-profit messages may also contain "exit")
        if _BOOK_PROFIT_KEYWORDS.search(cleaned):
            return self._parse_book_profit(cleaned, text, message_id, timestamp)
        if _EXIT_KEYWORDS.search(cleaned):
            return self._parse_exit(cleaned, text, message_id, timestamp)
        if _ENTRY_KEYWORDS.search(cleaned):
            return self._parse_entry(cleaned, text, message_id, timestamp)

        # Fallback: if the message contains instrument details + a price, treat as entry
        if _INSTRUMENT_RE.search(cleaned) and _RUPEE_PRICE_RE.search(cleaned):
            return self._parse_entry(cleaned, text, message_id, timestamp)

        return None

    # ── Private parsers ──────────────────────────────────────────────────

    def _parse_entry(
        self,
        cleaned: str,
        raw: str,
        message_id: Optional[int],
        timestamp: Optional[datetime],
    ) -> Optional[EntrySignal]:
        m = _INSTRUMENT_RE.search(cleaned)
        if not m:
            logger.debug("ENTRY keyword found but no instrument match: %s", cleaned[:80])
            return None

        underlying_raw = m.group("underlying").strip()
        underlying = _normalize_underlying(underlying_raw)
        day = int(m.group("day"))
        month = _normalise_month(m.group("month"))
        strike = float(m.group("strike"))
        otype = OptionType(m.group("otype").upper())

        # Extract entry price — look for "at <price>" after the instrument block
        entry_price = _extract_price_after(cleaned[m.end():], "")
        if entry_price is None:
            # Try "at ₹135" anywhere
            entry_price = _extract_price_after(cleaned, "at")
        if entry_price is None:
            # Try "@ 135"
            entry_price = _extract_price_after(cleaned, "@")
        if entry_price is None:
            logger.debug("ENTRY signal but no price found: %s", cleaned[:80])
            return None

        targets = _extract_targets(cleaned)
        stoploss = _extract_stoploss(cleaned)

        return EntrySignal(
            underlying=underlying,
            expiry_day=day,
            expiry_month=month,
            strike_price=strike,
            option_type=otype,
            entry_price=entry_price,
            targets=targets,
            stoploss=stoploss,
            raw_text=raw,
            message_id=message_id,
            timestamp=timestamp,
        )

    def _parse_exit(
        self,
        cleaned: str,
        raw: str,
        message_id: Optional[int],
        timestamp: Optional[datetime],
    ) -> Optional[ExitSignal]:
        m = _INSTRUMENT_RE.search(cleaned)

        # Even without full instrument details, try to extract just the underlying
        # e.g. "Please exit from NIFTY50 17 FEB"
        if m:
            underlying = _normalize_underlying(m.group("underlying").strip())
            return ExitSignal(
                underlying=underlying,
                expiry_day=int(m.group("day")),
                expiry_month=_normalise_month(m.group("month")),
                strike_price=float(m.group("strike")),
                option_type=OptionType(m.group("otype").upper()),
                raw_text=raw,
                message_id=message_id,
                timestamp=timestamp,
            )

        # Partial match: "Exit NIFTY50 17 FEB" without strike/otype
        partial = re.search(
            r"(?:exit|close|square\s*off)\s+(?:from\s+)?(?P<underlying>[A-Z][A-Z0-9]{1,20})"
            r"(?:\s+(?P<day>\d{1,2})\s+(?P<month>" + _MONTH_PATTERN + r"))?",
            cleaned,
            re.IGNORECASE,
        )
        if partial:
            underlying = _normalize_underlying(partial.group("underlying").strip())
            day = int(partial.group("day")) if partial.group("day") else None
            month = _normalise_month(partial.group("month")) if partial.group("month") else None
            return ExitSignal(
                underlying=underlying,
                expiry_day=day,
                expiry_month=month,
                raw_text=raw,
                message_id=message_id,
                timestamp=timestamp,
            )

        logger.debug("EXIT keyword found but no instrument match: %s", cleaned[:80])
        return None

    def _parse_book_profit(
        self,
        cleaned: str,
        raw: str,
        message_id: Optional[int],
        timestamp: Optional[datetime],
    ) -> Optional[BookProfitSignal]:
        m = _INSTRUMENT_RE.search(cleaned)

        # Try to extract exit price
        exit_price = _extract_price_after(cleaned, "price")
        if exit_price is None:
            exit_price = _extract_price_after(cleaned, "at")
        if exit_price is None:
            exit_price = _extract_price_after(cleaned, "@")

        if m:
            underlying = _normalize_underlying(m.group("underlying").strip())
            return BookProfitSignal(
                underlying=underlying,
                expiry_day=int(m.group("day")),
                expiry_month=_normalise_month(m.group("month")),
                strike_price=float(m.group("strike")),
                option_type=OptionType(m.group("otype").upper()),
                exit_price=exit_price,
                raw_text=raw,
                message_id=message_id,
                timestamp=timestamp,
            )

        # Partial: "Book Profit in INDHOTEL 24 FEB" (without strike)
        partial = re.search(
            r"(?:book\s+profit|take\s+profit)\s+(?:in\s+)?(?P<underlying>[A-Z][A-Z0-9]{1,20})"
            r"(?:\s+(?P<day>\d{1,2})\s+(?P<month>" + _MONTH_PATTERN + r"))?",
            cleaned,
            re.IGNORECASE,
        )
        if partial:
            underlying = _normalize_underlying(partial.group("underlying").strip())
            day = int(partial.group("day")) if partial.group("day") else None
            month = _normalise_month(partial.group("month")) if partial.group("month") else None
            return BookProfitSignal(
                underlying=underlying,
                expiry_day=day,
                expiry_month=month,
                exit_price=exit_price,
                raw_text=raw,
                message_id=message_id,
                timestamp=timestamp,
            )

        logger.debug("BOOK_PROFIT keyword found but no instrument match: %s", cleaned[:80])
        return None
