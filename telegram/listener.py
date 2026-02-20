"""Real-time Telegram group message listener using Telethon."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telethon import TelegramClient, events

from broker.base import BrokerInterface
from config.settings import Settings
from parser.models import TradeSignal
from parser.signal_parser import SignalParser
from storage.db import Database
from storage.daily_log import append_message

from config.log_config import (
    TAG_TELEGRAM, TAG_SIGNAL, TAG_UNPARSED, TAG_DB, format_signal_table,
)

logger = logging.getLogger(__name__)


class TelegramListener:
    """Connects to Telegram as a user account and listens for new messages
    in the configured group.  Each message is parsed and, if it contains a
    valid trade signal, forwarded to the broker for execution.

    Every incoming message is dispatched to its own ``asyncio.Task`` so
    that slow broker operations (Groww API calls, order verification)
    never block the reception of subsequent messages.
    """

    def __init__(
        self,
        settings: Settings,
        parser: SignalParser,
        broker: BrokerInterface,
        db: Database,
    ) -> None:
        self._settings = settings
        self._parser = parser
        self._broker = broker
        self._db = db
        self._client: Optional[TelegramClient] = None
        self._tasks: set[asyncio.Task] = set()

    # ── Public API ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create the Telethon client, register the handler, and start listening."""
        self._client = TelegramClient(
            self._settings.session_path,
            self._settings.telegram_api_id,
            self._settings.telegram_api_hash,
        )

        # Register the new-message handler *before* starting
        self._client.on(events.NewMessage(chats=self._settings.telegram_group_id))(
            self._on_new_message
        )

        await self._client.start()

        # Fetch dialogs so Telethon caches the entity for the numeric group ID.
        # Without this, numeric IDs fail with "Could not find the input entity".
        logger.info("%s Loading dialogs to resolve group entity …", TAG_TELEGRAM)
        await self._client.get_dialogs()

        me = await self._client.get_me()
        logger.info(
            "%s Listener started as %s (id=%s) — watching group %s",
            TAG_TELEGRAM, me.first_name if me else "?",
            me.id if me else "?", self._settings.telegram_group_id,
        )

    async def run_forever(self) -> None:
        """Block until the client disconnects."""
        if self._client is None:
            raise RuntimeError("Call start() before run_forever()")
        logger.info("%s Listening for trade signals … (Ctrl+C to stop)", TAG_TELEGRAM)
        await self._client.run_until_disconnected()

    async def stop(self) -> None:
        if self._tasks:
            logger.info("%s Waiting for %d in-flight message tasks …", TAG_TELEGRAM, len(self._tasks))
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        if self._client:
            await self._client.disconnect()
            logger.info("%s Listener stopped", TAG_TELEGRAM)

    # ── Event handler ────────────────────────────────────────────────────

    def _task_done(self, task: asyncio.Task) -> None:
        """Callback to remove finished tasks and log uncaught errors."""
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "Unhandled error in message task — skipping and continuing",
                exc_info=exc,
            )

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Dispatch each message to its own asyncio task for concurrent processing."""
        task = asyncio.create_task(self._safe_handle(event))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    async def _safe_handle(self, event: events.NewMessage.Event) -> None:
        """Top-level wrapper so no single message can crash the daemon."""
        try:
            await self._handle_message(event)
        except Exception:
            msg_id = getattr(getattr(event, "message", None), "id", "?")
            logger.exception(
                "Unhandled error processing message %s — skipping and continuing",
                msg_id,
            )

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        """Inner handler that does the actual work."""
        text: str = event.message.text or ""
        if not text.strip():
            return

        msg_id = event.message.id
        timestamp = event.message.date

        # Append every message to the mode-specific daily log file
        append_message(
            mode=self._settings.mode.value,
            msg_id=msg_id,
            timestamp=timestamp,
            text=text,
        )

        _SEP = "=" * 80
        preview = text.replace("\n", " ").strip()
        if len(preview) > 200:
            preview = preview[:200] + "…"

        logger.info("")
        logger.info(_SEP)
        logger.info("%s Original message (msg_id=%s): %s", TAG_TELEGRAM, msg_id, preview)
        logger.info("")

        signal: Optional[TradeSignal] = self._parser.parse(
            text, message_id=msg_id, timestamp=timestamp
        )
        if signal is None:
            logger.warning(
                "%s Parsing status: FAILED — could not parse signal from message",
                TAG_UNPARSED,
            )
            logger.info("")
            return

        logger.info(
            "%s Parsing status: OK — %s → %s",
            TAG_SIGNAL, signal.signal_type.value, signal.display_name,
        )
        for line in format_signal_table(signal).split("\n"):
            logger.info("%s %s", TAG_SIGNAL, line)
        logger.info("")

        if await self._db.is_signal_processed(signal.signal_hash):
            logger.info("%s Already processed (hash=%s), skipping", TAG_SIGNAL, signal.signal_hash)
            logger.info("")
            return

        try:
            signal_id = await self._db.insert_signal(signal)
        except Exception:
            logger.exception("%s Failed to insert signal into DB", TAG_DB)
            return

        try:
            await self._broker.execute(signal, signal_id)
        except Exception:
            logger.exception("%s Broker execution failed for signal %s", TAG_SIGNAL, signal.signal_hash)

        try:
            await self._db.mark_signal_processed(signal.signal_hash)
        except Exception:
            logger.exception("%s Failed to mark signal %s as processed", TAG_DB, signal.signal_hash)

        logger.info("")
