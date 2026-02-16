"""Daily message log rotation — writes every Telegram message to a
date-partitioned text file, separated by mode.

Directory layout::

    data/logs/
        live/
            live_2026-02-16.txt
            live_2026-02-17.txt
        paper/
            paper_2026-02-16.txt
        backtest/
            backtest_2026-02-16.txt

Each file uses the same format as ``fetch_messages.py``::

    === MSG #<id> | YYYY-MM-DD HH:MM:SS ===
    <message text>

This module is intentionally stateless — every function takes the mode
as an explicit argument so it can be used from the listener (live/paper)
and the backtest engine alike.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# IST = UTC+5:30
_IST = timezone(timedelta(hours=5, minutes=30))

# Root directory for all daily logs
_LOGS_ROOT = Path("data/logs")


def _to_ist(dt: datetime) -> datetime:
    """Convert *dt* to IST, handling both aware and naive datetimes."""
    if dt.tzinfo is not None:
        return dt.astimezone(_IST)
    return dt


def _log_dir_for_mode(mode: str) -> Path:
    """Return ``data/logs/<mode>/`` for the given mode string."""
    return _LOGS_ROOT / mode.lower()


def _log_filename(mode: str, date_str: str) -> str:
    """Return ``<mode>_<YYYY-MM-DD>.txt``."""
    return f"{mode.lower()}_{date_str}.txt"


def append_message(
    mode: str,
    msg_id: int,
    timestamp: datetime,
    text: str,
) -> None:
    """Append a single message to the daily log for *mode*.

    Creates the directory tree if it doesn't exist.  Errors are logged
    at DEBUG level and silently swallowed so they never disrupt the
    main processing pipeline.
    """
    try:
        log_dir = _log_dir_for_mode(mode)
        log_dir.mkdir(parents=True, exist_ok=True)

        ist_time = _to_ist(timestamp)
        date_str = ist_time.strftime("%Y-%m-%d")
        time_str = ist_time.strftime("%Y-%m-%d %H:%M:%S")
        log_path = log_dir / _log_filename(mode, date_str)

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"=== MSG #{msg_id} | {time_str} ===\n")
            f.write(text)
            f.write("\n\n")
    except Exception:
        logger.debug(
            "Failed to write message %s to %s daily log",
            msg_id, mode, exc_info=True,
        )


def get_daily_log_path(
    mode: str,
    date_str: str | None = None,
) -> Path | None:
    """Return the path to the daily log file for *mode* and *date_str*.

    Returns ``None`` if the file does not exist.
    """
    if date_str is None:
        date_str = datetime.now(_IST).strftime("%Y-%m-%d")
    log_path = _log_dir_for_mode(mode) / _log_filename(mode, date_str)
    return log_path if log_path.exists() else None
