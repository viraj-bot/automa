"""Regex-based parser that converts raw Telegram messages into structured trade signals.

Handles four signal types:
  1. ENTRY       – "Buy NIFTY50 17 FEB 25600 PE at 135"
  2. EXIT        – "Exit Nifty50 17 FEB 25600 PE at the current price 72.5"
  3. BOOK_PROFIT – "Book Profit in INDHOTEL 24 FEB 690 CE at price 17.2"
  4. PARTIAL BP  – "Book partial profit in PERSISTENT 27 JAN 6400 CE at 226.99"

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
_ACTION_WORDS = (
    "BUY", "SELL", "SHORT", "LONG", "EXIT", "EXITED", "CLOSE", "CLOSED",
    "BOOK", "BOOKED", "PROFIT", "TAKE", "TRAIL", "TRAILING", "PARTIAL",
    "NEW", "FRESH", "ENTER", "ENTRY", "INITIATE", "ADD",
    "PLEASE", "FROM", "IN", "ON", "AT", "THE", "OF", "FOR",
    "OPTION", "TRADE", "OPTION TRADE", "FULL",
    "GO", "GET", "OUT", "SQUARE", "SQUARED", "REMAINING",
    "TARGET", "HIT",
)
_ACTION_WORDS_SET = frozenset(_ACTION_WORDS)

# ── Noise / non-trade message filter ──
_NOISE_PATTERNS = re.compile(
    r"(?:"
    r"ADD\s+T(?:O|)\s*WATCHLIS|"          # ADD TO WATCHLIST (incl typos)
    r"WATCHLIST|"
    r"Pre-Market\s+Update|"
    r"Post-Market\s+Report|"
    r"POST\s+MARKET\s+REPORT|"
    r"AVOID\s+THE\s+TRADE|"
    r"Entry\s+Missed|"
    r"GIFT\s+Nifty|"
    r"Trade\s+Performance|"
    r"Market\s+Close|"
    r"Market\s+Snapshot|"
    r"COMMODITY\s+UPDATE|"
    r"MCX\s+Crude|"
    r"Union\s+Budget|"
    r"Hold\s+for\s+next\s+trading|"
    r"Hold\s+the\s+position"
    r")",
    re.IGNORECASE,
)

# ── Instrument pattern (shared across signal types) ──
_INSTRUMENT_RE = re.compile(
    r"(?P<underlying>[A-Z][A-Z0-9]{1,20}(?:\s+[A-Z][A-Z0-9]{1,20})?)\s+"
    r"(?P<day>\d{1,2})\s+"
    rf"(?P<month>{_MONTH_PATTERN})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s*"
    r"(?P<otype>CE|PE)",
    re.IGNORECASE,
)


def _clean_underlying(raw_match: str, full_text: str, match_start: int) -> str:
    """Strip leading action keywords from a captured underlying group."""
    parts = raw_match.strip().split()

    while len(parts) > 1 and parts[0].upper() in _ACTION_WORDS_SET:
        parts = parts[1:]

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

# ── Exit price patterns (specific to EXIT messages) ──

# "at the current price ₹72.5" or "at the current price: ₹72.5"
_EXIT_CURRENT_PRICE_RE = re.compile(
    r"(?:at\s+the\s+)?current\s+price\s*[:=]?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# "at cost to cost ₹30.6" or "cost-to-cost ₹30.6"
_EXIT_CTC_PRICE_RE = re.compile(
    r"(?:at\s+)?cost\s*(?:to|2)\s*cost\s*[:=]?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# "Exit Remaining quantity at ₹31.15"
_EXIT_REMAINING_PRICE_RE = re.compile(
    r"(?:exit\s+)?remaining\s+(?:quantity|qty)\s+at\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Generic "at ₹<price>" or "at price ₹<price>" — used as last resort for exits
_EXIT_AT_PRICE_RE = re.compile(
    r"\bat\s+(?:price\s+)?(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _extract_exit_price(text: str) -> Optional[float]:
    """Extract exit price from an EXIT or BOOK_PROFIT message.

    Tries patterns in order of specificity:
    1. "at the current price ₹72.5"
    2. "at cost to cost ₹30.6"
    3. "Exit Remaining quantity at ₹31.15"
    4. "at price ₹237" / "at ₹237"
    """
    for pattern in (
        _EXIT_CURRENT_PRICE_RE,
        _EXIT_CTC_PRICE_RE,
        _EXIT_REMAINING_PRICE_RE,
        _EXIT_AT_PRICE_RE,
    ):
        m = pattern.search(text)
        if m:
            return float(m.group(1))
    return None


# ── Target extraction regexes ──

_TARGET_LABELLED_RE = re.compile(
    r"\b(?:target|tgt|tp)\s*(\d{1,2})\s*[:=\-–—]?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_TARGET_LIST_RE = re.compile(
    r"(?:targets?|tgt|tp)\s*[:=\-–—]\s*"
    r"((?:(?:₹|rs\.?|inr)?\s*\d+(?:\.\d+)?\s*[/,\s]\s*)*"
    r"(?:₹|rs\.?|inr)?\s*\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_TARGET_BARE_RE = re.compile(
    r"(?:target|tgt)\s+(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_TARGET_EMOJI_RE = re.compile(
    r"🎯\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
)

_PRICE_IN_LIST_RE = re.compile(r"(\d+(?:\.\d+)?)")

_STOPLOSS_RE = re.compile(
    r"(?:stoploss|stop\s*loss|sl|stop)\s*(?:at|@|[:=\-–—])?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_STOPLOSS_EMOJI_RE = re.compile(
    r"[🛑❌⛔]\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
)


def _clean_text(text: str) -> str:
    """Remove emojis, markdown formatting, URLs, collapse whitespace, strip."""
    text = _EMOJI_RE.sub(" ", text)
    text = text.replace("**", "")           # strip Telegram markdown bold
    text = text.replace("__", "")           # strip Telegram markdown italic
    text = text.replace("``", "")           # strip Telegram markdown code
    text = re.sub(r"https?://\S+", " ", text)  # strip URLs
    text = re.sub(r"[^\S\n]+", " ", text)   # collapse horizontal whitespace
    return text.strip()


_CMP_PRICE_RE = re.compile(
    r"(?:cmp|around|near|above|below)\s*(?:at|@|[:=\-])?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_RANGE_PRICE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:range|zone)?",
    re.IGNORECASE,
)


def _extract_targets(text: str) -> list[float]:
    """Extract all target prices from the text."""
    labelled = [float(m.group(2)) for m in _TARGET_LABELLED_RE.finditer(text)]
    if labelled:
        return labelled

    list_match = _TARGET_LIST_RE.search(text)
    if list_match:
        raw_list = list_match.group(1)
        prices = [float(p.group(1)) for p in _PRICE_IN_LIST_RE.finditer(raw_list)]
        if prices:
            return prices

    bare = [float(m.group(1)) for m in _TARGET_BARE_RE.finditer(text)]
    if bare:
        return bare

    emoji_targets = [float(m.group(1)) for m in _TARGET_EMOJI_RE.finditer(text)]
    if emoji_targets:
        return emoji_targets

    return []


def _extract_stoploss(text: str) -> Optional[float]:
    m = _STOPLOSS_RE.search(text)
    if m:
        return float(m.group(1))
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
    r"please\s+exit|exit\s+from|get\s+out|out\s+of|"
    r"exit\s+remaining|remaining\s+quantity)\b",
    re.IGNORECASE,
)

_BOOK_PROFIT_KEYWORDS = re.compile(
    r"\b(book\s+prof(?:it|t)|booked\s+prof(?:it|t)|profit\s+booked|"
    r"partial\s+profit|partial\s+exit|trail\s+sl|trailing\s+sl|"
    r"book\s+partial|take\s+profit|target\s+\d+\s+hit|"
    r"book\s+full\s+profit)\b",
    re.IGNORECASE,
)

# ── Partial vs full book-profit detection ──

_PARTIAL_INDICATORS = re.compile(
    r"(?:partial\s+profit|book\s+partial|partial\s+lot|"
    r"target\s+1\s+(?:hit|reached)|move\s+(?:your\s+)?stop\s*loss\s+to\s+(?:your\s+)?cost|"
    r"hold\s+the\s+remaining|remaining\s+quantity)",
    re.IGNORECASE,
)

_FULL_INDICATORS = re.compile(
    r"(?:book\s+full\s+profit|target\s+2\s+(?:hit|reached)|"
    r"book\s+profit\s+now|full\s+profit)",
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

        Returns ``None`` if the message is not recognised as a trade signal
        **or** if an unexpected error occurs during parsing (logged and
        swallowed so the caller never crashes).
        """
        try:
            return self._parse_inner(text, message_id, timestamp)
        except Exception:
            preview = (text or "").replace("\n", " ").strip()[:120]
            logger.exception(
                "Parser error on msg_id=%s — treating as unparsed: %s",
                message_id, preview,
            )
            return None

    def _parse_inner(
        self,
        text: str,
        message_id: Optional[int],
        timestamp: Optional[datetime],
    ) -> Optional[TradeSignal]:
        """Core parsing logic (may raise on malformed input)."""
        if not text or len(text) < 5:
            return None

        # ── Noise filter: skip non-trade messages early ──
        if _NOISE_PATTERNS.search(text):
            return None

        cleaned = _clean_text(text)

        # Determine signal type by keyword priority:
        # BOOK_PROFIT > EXIT > ENTRY  (book-profit messages may also contain "exit")
        #
        # Special case: "Exit Remaining quantity" messages mention "partial profit"
        # as a note ("Partial profit already booked earlier") but are actually EXIT
        # signals.  Check for "exit remaining" before book-profit keywords.
        has_exit_remaining = bool(re.search(
            r"exit\s+remaining", cleaned, re.IGNORECASE
        ))
        if _BOOK_PROFIT_KEYWORDS.search(cleaned) and not has_exit_remaining:
            return self._parse_book_profit(cleaned, text, message_id, timestamp)
        if _EXIT_KEYWORDS.search(cleaned):
            return self._parse_exit(cleaned, text, message_id, timestamp)
        if _ENTRY_KEYWORDS.search(cleaned):
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

        # Extract entry price — try multiple patterns
        entry_price = None
        after_instrument = cleaned[m.end():]

        # "at ₹135" or "above ₹246" right after instrument
        at_m = re.search(
            r"(?:at|above|below|@)\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
            after_instrument,
            re.IGNORECASE,
        )
        if at_m:
            entry_price = float(at_m.group(1))

        if entry_price is None:
            # Try "CMP 135", "around 135", "near 135"
            cmp_m = _CMP_PRICE_RE.search(cleaned)
            if cmp_m:
                entry_price = float(cmp_m.group(1))

        if entry_price is None:
            # Try "135-140 range" — use midpoint
            range_m = _RANGE_PRICE_RE.search(after_instrument)
            if range_m:
                lo = float(range_m.group(1))
                hi = float(range_m.group(2))
                entry_price = (lo + hi) / 2

        if entry_price is None:
            logger.debug("ENTRY signal but no price found: %s", cleaned[:80])
            return None

        # Extract targets/SL from cleaned text first, then raw text
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
        # Extract exit price from the message
        exit_price = _extract_exit_price(cleaned)

        m = _INSTRUMENT_RE.search(cleaned)
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
                exit_price=exit_price,
                raw_text=raw,
                message_id=message_id,
                timestamp=timestamp,
            )

        # Partial match: "Exit NIFTY50 17 FEB"
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
                exit_price=exit_price,
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

        # Extract exit price
        exit_price = _extract_exit_price(cleaned)

        # Detect partial vs full
        is_partial = False
        if _PARTIAL_INDICATORS.search(cleaned):
            is_partial = True
        if _FULL_INDICATORS.search(cleaned):
            is_partial = False  # explicit "full" overrides partial

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
                is_partial=is_partial,
                raw_text=raw,
                message_id=message_id,
                timestamp=timestamp,
            )

        # Partial match: "Book Profit in INDHOTEL 24 FEB"
        partial = re.search(
            r"(?:book(?:ed)?\s+(?:partial\s+)?prof(?:it|t)|take\s+profit|"
            r"profit\s+booked|partial\s+exit|book\s+full\s+profit)"
            r"\s+(?:in\s+|on\s+|for\s+)?(?P<underlying>[A-Z][A-Z0-9]{1,20}(?:\s+[A-Z][A-Z0-9]{1,20})?)"
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
                is_partial=is_partial,
                raw_text=raw,
                message_id=message_id,
                timestamp=timestamp,
            )

        logger.debug("BOOK_PROFIT keyword found but no instrument match: %s", cleaned[:80])
        return None
