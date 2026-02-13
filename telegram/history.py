"""Fetch historical messages from a Telegram group for backtesting."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telethon import TelegramClient

from config.settings import Settings

logger = logging.getLogger(__name__)


async def fetch_chat_history(
    settings: Settings,
    days: int = 30,
    limit: Optional[int] = None,
) -> list[dict]:
    """Download messages from the configured Telegram group.

    Parameters
    ----------
    settings:
        Application settings (contains Telegram credentials and group ID).
    days:
        How many days of history to fetch (from today backwards).
    limit:
        Maximum number of messages to return.  ``None`` = no limit.

    Returns
    -------
    list[dict]
        Each dict has keys: ``id``, ``date``, ``text``.
        Sorted oldest-first.
    """
    client = TelegramClient(
        settings.session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )

    await client.start()
    logger.info("Connected to Telegram for history fetch")

    offset_date = datetime.now(tz=timezone.utc) - timedelta(days=days)
    messages: list[dict] = []

    async for msg in client.iter_messages(
        settings.telegram_group_id,
        offset_date=offset_date,
        reverse=True,  # oldest first
        limit=limit,
    ):
        if msg.text:
            messages.append(
                {
                    "id": msg.id,
                    "date": msg.date,
                    "text": msg.text,
                }
            )

    await client.disconnect()
    logger.info("Fetched %d messages from the last %d days", len(messages), days)
    return messages
