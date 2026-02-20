"""Centralised logging configuration for Automa.

Provides:
- IST timestamps in DD-MM-YYYY HH:MM:SS format
- Color-coded console output via Rich (level + category colors)
- Structured, plain-text file logging with daily rotation
- Consistent tag/category system across all modules

Usage (from main.py):
    from config.log_config import setup_logging
    setup_logging(level="INFO", mode="live")
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.logging import RichHandler
from rich.highlighter import RegexHighlighter
from rich.theme import Theme
from rich.console import Console

IST = timezone(timedelta(hours=5, minutes=30))

# ── Tag constants ────────────────────────────────────────────────────────────
# Use these in log messages for consistent, grep-friendly output.

TAG_GROWW_REQ = "[GROWW REQ]"
TAG_GROWW_RES = "[GROWW RES]"
TAG_GROWW_ERR = "[GROWW ERR]"
TAG_LIVE = "[LIVE]"
TAG_PAPER = "[PAPER]"
TAG_BT = "[BT]"
TAG_SIGNAL = "[SIGNAL]"
TAG_TELEGRAM = "[TELEGRAM]"
TAG_DB = "[DB]"
TAG_SCHEDULER = "[SCHEDULER]"
TAG_UNPARSED = "[UNPARSED]"
TAG_RISK = "[RISK]"
TAG_CRITICAL = "[CRITICAL]"


# ── IST Formatter ────────────────────────────────────────────────────────────

class ISTFormatter(logging.Formatter):
    """Formats timestamps in IST regardless of system timezone, with ms precision."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=IST)
        base = dt.strftime(datefmt or "%d-%m-%Y %H:%M:%S")
        ms = int(record.msecs)
        return f"{base}.{ms:03d}"


# ── Custom highlighter for trade-specific patterns ───────────────────────────

class _TradeHighlighter(RegexHighlighter):
    base_style = "automa."
    highlights = [
        r"(?P<tag_groww>\[GROWW (?:REQ|RES|ERR)\])",
        r"(?P<tag_live>\[LIVE\])",
        r"(?P<tag_paper>\[PAPER\])",
        r"(?P<tag_bt>\[BT\])",
        r"(?P<tag_signal>\[SIGNAL\])",
        r"(?P<tag_telegram>\[TELEGRAM\])",
        r"(?P<tag_db>\[DB\])",
        r"(?P<tag_scheduler>\[SCHEDULER\])",
        r"(?P<tag_unparsed>\[UNPARSED\])",
        r"(?P<tag_risk>\[RISK\])",
        r"(?P<tag_critical>\[CRITICAL\])",
        r"(?P<price>₹[\d,]+\.?\d*)",
        r"(?P<profit>\bP&L: ₹-?[\d,]+\.?\d*)",
        r"(?P<action_buy>\bBUY\b)",
        r"(?P<action_sell>\bSELL\b|\bEXIT\b|\bBOOK_PROFIT\b|\bPARTIAL_BP\b)",
        r"(?P<order_id>order_id=\S+)",
        r"(?P<symbol>NSE-[A-Z]+-\S+)",
    ]


_THEME = Theme({
    "automa.tag_groww": "bold cyan",
    "automa.tag_live": "bold green",
    "automa.tag_paper": "bold blue",
    "automa.tag_bt": "bold magenta",
    "automa.tag_signal": "bold yellow",
    "automa.tag_telegram": "bold blue",
    "automa.tag_db": "dim cyan",
    "automa.tag_scheduler": "bold white",
    "automa.tag_unparsed": "bold bright_black",
    "automa.tag_risk": "bold red",
    "automa.tag_critical": "bold white on red",
    "automa.price": "cyan",
    "automa.profit": "bold green",
    "automa.action_buy": "bold green",
    "automa.action_sell": "bold red",
    "automa.order_id": "dim",
    "automa.symbol": "bold white",
    "logging.level.debug": "dim cyan",
    "logging.level.info": "bold green",
    "logging.level.warning": "bold yellow",
    "logging.level.error": "bold red",
    "logging.level.critical": "bold white on red",
})


# ── File format (plain text, structured for grep/tail) ──────────────────────

_FILE_FMT = "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s"
_FILE_DATEFMT = "%d-%m-%Y %H:%M:%S"

_CONSOLE_FMT = "%(message)s"
_CONSOLE_DATEFMT = "%d-%m-%Y %H:%M:%S"


# ── Public API ───────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO", mode: str = "paper") -> None:
    """Configure root logger with Rich console + file handler.

    Console: color-coded with trade-aware highlighting.
    File:    plain text at ``data/logs/<mode>/<mode>_YYYY-MM-DD.log``.
    """
    console = Console(theme=_THEME, stderr=True)

    console_handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        markup=True,
        show_time=False,
        show_level=True,
        show_path=False,
        highlighter=_TradeHighlighter(),
    )

    console_handler.setFormatter(ISTFormatter(
        "%(asctime)s  %(message)s", datefmt=_CONSOLE_DATEFMT,
    ))

    handlers: list[logging.Handler] = [console_handler]

    log_dir = Path("data/logs") / mode.lower()
    log_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(IST).strftime("%Y-%m-%d")
    log_file = log_dir / f"{mode.lower()}_{date_str}.log"

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(ISTFormatter(_FILE_FMT, datefmt=_FILE_DATEFMT))
    handlers.append(file_handler)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("growwapi").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ── Signal table formatter ──────────────────────────────────────────────────
def format_signal_table(signal) -> str:
    """Build a compact ASCII table summarising a parsed trade signal.

    Works with EntrySignal, ExitSignal, and BookProfitSignal without
    importing them (uses duck-typing on common attributes).
    """
    from parser.models import EntrySignal, ExitSignal, BookProfitSignal

    W = 52
    border = "+" + "-" * (W - 2) + "+"
    title = f" {signal.signal_type.value} Signal "
    title_line = f"|{title:=^{W - 2}}|"

    def row(label: str, value: str) -> str:
        inner = f"  {label:<16}: {value}"
        return f"|{inner:<{W - 2}}|"

    lines = [border, title_line, border]
    lines.append(row("Instrument", signal.display_name))

    if isinstance(signal, EntrySignal):
        lines.append(row("Entry Price", f"₹{signal.entry_price:,.2f}"))
        lines.append(row("Stoploss", f"₹{signal.stoploss:,.2f}" if signal.stoploss else "—"))
        if signal.stoploss:
            sl_pct = (signal.entry_price - signal.stoploss) / signal.entry_price * 100
            lines.append(row("SL %", f"{sl_pct:.1f}%"))
        if signal.targets:
            tgt_str = " / ".join(f"₹{t:,.0f}" for t in signal.targets)
            lines.append(row("Targets", tgt_str))
        lines.append(row("Option", signal.option_type.value))
        lines.append(row("Expiry", f"{signal.expiry_day} {signal.expiry_month}"))

    elif isinstance(signal, ExitSignal):
        lines.append(row("Exit Price", f"₹{signal.exit_price:,.2f}" if signal.exit_price else "MARKET"))
        if signal.option_type:
            lines.append(row("Option", signal.option_type.value))

    elif isinstance(signal, BookProfitSignal):
        lines.append(row("Exit Price", f"₹{signal.exit_price:,.2f}" if signal.exit_price else "MARKET"))
        lines.append(row("Type", "PARTIAL" if signal.is_partial else "FULL"))
        if signal.option_type:
            lines.append(row("Option", signal.option_type.value))

    lines.append(border)
    return "\n".join(lines)
