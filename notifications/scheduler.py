"""Lightweight async scheduler for the daily summary email.

Runs as a background ``asyncio.Task`` alongside the Telegram listener.
Fires the daily summary at the configured time on NSE market days only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta, time as dt_time

from config.settings import Settings
from notifications.daily_summary import generate_and_send_daily_summary
from storage.db import Database

logger = logging.getLogger(__name__)

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

# NSE holidays for 2026 (gazetted).
# Update this set at the start of each year.
_NSE_HOLIDAYS_2026 = {
    "2026-01-26",  # Republic Day
    "2026-02-17",  # Mahashivratri (tentative)
    "2026-03-10",  # Holi
    "2026-03-30",  # Id-Ul-Fitr (tentative)
    "2026-03-31",  # Id-Ul-Fitr (tentative)
    "2026-04-02",  # Ram Navami
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-25",  # Buddha Purnima
    "2026-06-05",  # Bakri Id (tentative)
    "2026-07-06",  # Muharram (tentative)
    "2026-08-15",  # Independence Day
    "2026-08-19",  # Janmashtami
    "2026-09-04",  # Milad-un-Nabi (tentative)
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-10-21",  # Dussehra
    "2026-11-09",  # Diwali (Laxmi Puja)
    "2026-11-10",  # Diwali (Balipratipada)
    "2026-11-30",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
}


def is_market_day(dt: datetime | None = None) -> bool:
    """Return True if *dt* (default: now in IST) is an NSE trading day.

    A market day is a weekday (Mon–Fri) that is not an NSE holiday.
    """
    if dt is None:
        dt = datetime.now(IST)
    date_str = dt.strftime("%Y-%m-%d")
    # Weekend check: Monday=0, Sunday=6
    if dt.weekday() >= 5:
        return False
    if date_str in _NSE_HOLIDAYS_2026:
        return False
    return True


def _parse_time(time_str: str) -> dt_time:
    """Parse 'HH:MM' into a time object."""
    parts = time_str.strip().split(":")
    return dt_time(int(parts[0]), int(parts[1]))


def _seconds_until(target: dt_time) -> float:
    """Return seconds from now (IST) until the next occurrence of *target*.

    If the target time has already passed today, returns seconds until
    that time tomorrow.
    """
    now = datetime.now(IST)
    target_dt = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if target_dt <= now:
        # Already passed today — schedule for tomorrow
        target_dt += timedelta(days=1)
    return (target_dt - now).total_seconds()


async def run_daily_summary_scheduler(
    settings: Settings,
    db: Database,
    shutdown_event: asyncio.Event,
) -> None:
    """Background loop that fires the daily summary at the configured time.

    Runs until *shutdown_event* is set.  Sleeps in short intervals so it
    can respond to shutdown within a few seconds.
    """
    if not settings.daily_summary_enabled:
        logger.info("Daily summary is disabled — scheduler not started")
        return

    target_time = _parse_time(settings.daily_summary_time)
    logger.info(
        "Daily summary scheduler started — will fire at %s IST on market days",
        settings.daily_summary_time,
    )

    last_sent_date: str | None = None

    while not shutdown_event.is_set():
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")

        # Already sent today?
        if last_sent_date == today_str:
            # Sleep until tomorrow's target time
            wait = _seconds_until(target_time)
            logger.debug("Summary already sent today — sleeping %.0f seconds", wait)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=min(wait, 60))
                break
            except asyncio.TimeoutError:
                continue

        # How long until target time?
        wait = _seconds_until(target_time)

        # If more than 2 minutes away, sleep in 60-second chunks
        if wait > 120:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                continue

        # Within 2 minutes — sleep precisely
        if wait > 0:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

        # Time to fire!
        if not is_market_day():
            logger.info("Not a market day (%s) — skipping daily summary", today_str)
            last_sent_date = today_str
            continue

        logger.info("Firing daily summary for %s …", today_str)
        try:
            await generate_and_send_daily_summary(settings, db)
            last_sent_date = today_str
        except Exception:
            logger.exception("Daily summary failed — will retry in 5 minutes")
            # Wait 5 minutes before retrying
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=300)
                break
            except asyncio.TimeoutError:
                continue

    logger.info("Daily summary scheduler stopped")
