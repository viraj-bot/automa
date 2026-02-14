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

# ── Words that must NOT be captured as the underlying symbol ──
# These are action/filler keywords that precede the real instrument name.
_ACTION_WORDS = (
    "BUY", "SELL", "SHORT", "LONG", "EXIT", "EXITED", "CLOSE", "CLOSED",
    "BOOK", "BOOKED", "PROFIT", "TAKE", "TRAIL", "TRAILING", "PARTIAL",
    "NEW", "FRESH", "ENTER", "ENTRY", "INITIATE", "ADD",
    "PLEASE", "FROM", "IN", "ON", "AT", "THE", "OF", "FOR",
    "OPTION", "TRADE", "OPTION TRADE",
    "GO", "GET", "OUT", "SQUARE", "SQUARED",
)
_ACTION_WORDS_SET = frozenset(_ACTION_WORDS)

# ── Instrument pattern (shared across signal types) ──
# Matches: NIFTY50 17 FEB 25600 PE  or  INDHOTEL 24 FEB 690 CE
#          BANK NIFTY 17 FEB 25600 PE  (two-word underlyings)
# Groups:  underlying, day, month, strike, option_type
# The underlying group allows an optional second word so that
# "BANK NIFTY", "FIN NIFTY" etc. are captured as a single group
# and later normalised via _normalize_underlying().
#
# IMPORTANT: A raw regex alone cannot exclude action keywords from the
# underlying group reliably (negative lookahead for variable-length
# alternations is fragile).  Instead, we use a broad regex and post-filter
# the captured underlying in _clean_underlying().
_INSTRUMENT_RE = re.compile(
    r"(?P<underlying>[A-Z][A-Z0-9]{1,20}(?:\s+[A-Z][A-Z0-9]{1,20})?)\s+"
    r"(?P<day>\d{1,2})\s+"
    rf"(?P<month>{_MONTH_PATTERN})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s*"
    r"(?P<otype>CE|PE)",
    re.IGNORECASE,
)


def _clean_underlying(raw_match: str, full_text: str, match_start: int) -> str:
    """Strip leading action keywords from a captured underlying group.

    The instrument regex may capture "BUY NIFTY50" or "EXIT INDHOTEL" as the
    underlying because the two-word pattern is greedy.  This function peels
    off the first word if it is a known action keyword, leaving just the real
    instrument name (e.g. "NIFTY50", "INDHOTEL", "BANK NIFTY").

    It also looks *backwards* in the text for an additional preceding word
    that might be part of a two-word underlying (e.g. if the regex only
    captured "NIFTY" but "BANK" precedes it).
    """
    parts = raw_match.strip().split()

    # If the first word is an action keyword, drop it
    while len(parts) > 1 and parts[0].upper() in _ACTION_WORDS_SET:
        parts = parts[1:]

    # If we're left with a single word, check if the word before the match
    # in the original text forms a two-word underlying (e.g. "BANK" before "NIFTY")
    if len(parts) == 1:
        prefix_text = full_text[:match_start].rstrip()
        if prefix_text:
            last_word = prefix_text.split()[-1].upper()
            combined = f"{last_word} {parts[0].upper()}"
            from parser.models import UNDERLYING_ALIASES
            if combined in UNDERLYING_ALIASES:
                return combined

    return " ".join(parts)

# ── Price extraction helpers ──
_RUPEE_PRICE_RE = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# ── Target extraction regexes (multiple strategies) ──

# Strategy 1: Labelled targets like "target 1: 185", "T1: 185", "tgt1 - 185"
_TARGET_LABELLED_RE = re.compile(
    r"\b(?:target|tgt|tp)\s*(\d{1,2})\s*[:=\-–—]?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Strategy 2: List format like "targets: ₹60 / ₹90 / ₹120" or "tgt: 60, 90, 120"
_TARGET_LIST_RE = re.compile(
    r"(?:targets?|tgt|tp)\s*[:=\-–—]\s*"
    r"((?:(?:₹|rs\.?|inr)?\s*\d+(?:\.\d+)?\s*[/,\s]\s*)*"
    r"(?:₹|rs\.?|inr)?\s*\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Strategy 3: "target 185" or "tgt 185" (keyword followed directly by price, no number)
_TARGET_BARE_RE = re.compile(
    r"(?:target|tgt)\s+(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Strategy 4: Emoji-based targets like "🎯 185" or "🎯185/235"
_TARGET_EMOJI_RE = re.compile(
    r"🎯\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
)

# Extracts individual numeric values from a matched target-list string.
_PRICE_IN_LIST_RE = re.compile(r"(\d+(?:\.\d+)?)")

_STOPLOSS_RE = re.compile(
    r"(?:stoploss|stop\s*loss|sl|stop)\s*(?:at|@|[:=\-–—])?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Emoji-based stoploss: "🛑 90" or "❌ 90"
_STOPLOSS_EMOJI_RE = re.compile(
    r"[🛑❌⛔]\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
)


def _clean_text(text: str) -> str:
    """Remove emojis, markdown formatting, URLs, collapse whitespace, strip."""
    text = _EMOJI_RE.sub(" ", text)
    text = text.replace("**", "")           # strip Telegram markdown bold
    text = text.replace("__", "")           # strip Telegram markdown italic
    text = text.replace("``", "")           # strip Telegram markdown code
    text = re.sub(r"https?://\S+", " ", text)  # strip URLs to avoid false matches
    text = re.sub(r"[^\S\n]+", " ", text)   # collapse horizontal whitespace
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


# Additional price-keyword patterns for entry price extraction
_CMP_PRICE_RE = re.compile(
    r"(?:cmp|around|near|above|below)\s*(?:at|@|[:=\-])?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_RANGE_PRICE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:range|zone)?",
    re.IGNORECASE,
)


def _extract_targets(text: str) -> list[float]:
    """Extract all target prices from the text.

    Tries multiple strategies in order of specificity:
      1. Labelled:  "target 1: 185", "T1: 185", "tgt1 - 185"
      2. List:      "targets: ₹60 / ₹90 / ₹120" or "tgt: 60, 90, 120"
      3. Bare:      "target 185" (keyword + price, no number)
      4. Emoji:     "🎯 185"
    """
    # Strategy 1: labelled targets  (target 1: 185, T1: 185, …)
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

    # Strategy 3: bare "target 185" without a number
    bare = [float(m.group(1)) for m in _TARGET_BARE_RE.finditer(text)]
    if bare:
        return bare

    # Strategy 4: emoji-based "🎯 185"
    emoji_targets = [float(m.group(1)) for m in _TARGET_EMOJI_RE.finditer(text)]
    if emoji_targets:
        return emoji_targets

    return []


def _extract_stoploss(text: str) -> Optional[float]:
    m = _STOPLOSS_RE.search(text)
    if m:
        return float(m.group(1))
    # Try emoji-based stoploss
    m = _STOPLOSS_EMOJI_RE.search(text)
    if m:
        return float(m.group(1))
    return None


def _normalise_month(raw: str) -> str:
    return _MONTHS.get(raw.upper(), raw.upper()[:3])


# ── Signal-type keyword detectors ──

_ENTRY_KEYWORDS = re.compile(
    r"\b(buy|sell|short|new\s+option\s+trade|fresh\s+entry|enter|long|"
    r"go\s+long|go\s+short|initiate|add\s+more|add\s+position)\b",
    re.IGNORECASE,
)

_EXIT_KEYWORDS = re.compile(
    r"\b(exit|exited|close|closed|square\s*off|squared\s+off|"
    r"please\s+exit|exit\s+from|get\s+out|out\s+of)\b",
    re.IGNORECASE,
)

_BOOK_PROFIT_KEYWORDS = re.compile(
    r"\b(book\s+prof(?:it|t)|booked\s+prof(?:it|t)|profit\s+booked|"
    r"partial\s+profit|partial\s+exit|trail\s+sl|trailing\s+sl|"
    r"book\s+partial|take\s+profit)\b",
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

        underlying_raw = _clean_underlying(m.group("underlying"), cleaned, m.start())
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
            # Try "CMP 135", "around 135", "near 135", etc.
            cmp_m = _CMP_PRICE_RE.search(cleaned)
            if cmp_m:
                entry_price = float(cmp_m.group(1))
        if entry_price is None:
            # Try "135-140 range" — use midpoint
            range_m = _RANGE_PRICE_RE.search(cleaned[m.end():])
            if range_m:
                lo = float(range_m.group(1))
                hi = float(range_m.group(2))
                entry_price = (lo + hi) / 2
        if entry_price is None:
            logger.debug("ENTRY signal but no price found: %s", cleaned[:80])
            return None

        # Try extracting targets/SL from cleaned text first, then raw text
        # (raw text preserves emojis that _clean_text strips)
        targets = _extract_targets(cleaned) or _extract_targets(raw)
        stoploss = _extract_stoploss(cleaned) or _extract_stoploss(raw)

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
            underlying = _normalize_underlying(
                _clean_underlying(m.group("underlying"), cleaned, m.start())
            )
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

        # Partial match: "Exit NIFTY50 17 FEB" or "Exit BANK NIFTY 17 FEB"
        partial = re.search(
            r"(?:exit|close|square\s*off|exited|closed|squared\s+off|out\s+of)"
            r"\s+(?:from\s+)?(?P<underlying>[A-Z][A-Z0-9]{1,20}(?:\s+[A-Z][A-Z0-9]{1,20})?)"
            r"(?:\s+(?P<day>\d{1,2})\s+(?P<month>" + _MONTH_PATTERN + r"))?",
            cleaned,
            re.IGNORECASE,
        )
        if partial:
            underlying = _normalize_underlying(
                _clean_underlying(partial.group("underlying"), cleaned, partial.start("underlying"))
            )
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
            underlying = _normalize_underlying(
                _clean_underlying(m.group("underlying"), cleaned, m.start())
            )
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

        # Partial: "Book Profit in INDHOTEL 24 FEB" or "Booked profit BANK NIFTY"
        partial = re.search(
            r"(?:book(?:ed)?\s+prof(?:it|t)|take\s+profit|profit\s+booked|partial\s+exit)"
            r"\s+(?:in\s+|on\s+)?(?P<underlying>[A-Z][A-Z0-9]{1,20}(?:\s+[A-Z][A-Z0-9]{1,20})?)"
            r"(?:\s+(?P<day>\d{1,2})\s+(?P<month>" + _MONTH_PATTERN + r"))?",
            cleaned,
            re.IGNORECASE,
        )
        if partial:
            underlying = _normalize_underlying(
                _clean_underlying(partial.group("underlying"), cleaned, partial.start("underlying"))
            )
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
