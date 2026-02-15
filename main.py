#!/usr/bin/env python3
"""Automa — Telegram trade signal executor with Groww API.

Usage
-----
    python main.py                      # uses MODE from .env (default: paper)
    python main.py --mode live          # real orders on Groww
    python main.py --mode paper         # paper trading (no real money)
    python main.py --mode backtest --days 30   # replay last 30 days
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from rich.logging import RichHandler


def _setup_ssl_ca_bundle() -> None:
    """Auto-detect and configure a custom CA bundle for corporate proxies.

    If ``data/ca-bundle.pem`` exists (created by the user to include their
    corporate root CA alongside the standard certifi bundle), set the
    ``REQUESTS_CA_BUNDLE`` and ``SSL_CERT_FILE`` env vars so that the
    ``requests`` / ``urllib3`` / ``aiohttp`` libraries trust it.
    """
    custom_bundle = Path("data/ca-bundle.pem")
    if custom_bundle.exists() and custom_bundle.stat().st_size > 0:
        bundle_path = str(custom_bundle.resolve())
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle_path)
        os.environ.setdefault("SSL_CERT_FILE", bundle_path)


_setup_ssl_ca_bundle()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, markup=True)],
    )
    # Quiet noisy libraries
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("growwapi").setLevel(logging.WARNING)


async def _run_live_or_paper(mode: str) -> None:
    """Start the Telegram listener with either the live or paper broker.

    Includes automatic reconnection with exponential backoff so that
    transient network failures or Telegram disconnects do not kill the
    long-running daemon.
    """
    from config.settings import AppMode, get_settings
    from broker.paper_broker import PaperBroker
    from broker.groww_broker import GrowwBroker
    from parser.signal_parser import SignalParser
    from storage.db import Database
    from telegram.listener import TelegramListener

    log = logging.getLogger(__name__)
    settings = get_settings()
    settings_mode = AppMode(mode)

    db = Database(settings.database_path)
    await db.connect()

    parser = SignalParser()

    if settings_mode == AppMode.LIVE:
        broker = GrowwBroker(settings, db)
    else:
        broker = PaperBroker(settings, db)

    await broker.initialize()

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("Shutdown signal received …")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    # ── Reconnection loop ──
    max_backoff = 300  # 5 minutes
    backoff = 5        # start at 5 seconds

    while not shutdown_event.is_set():
        listener = TelegramListener(
            settings=settings,
            parser=parser,
            broker=broker,
            db=db,
        )

        try:
            await listener.start()
            backoff = 5  # reset on successful connect

            listener_task = asyncio.create_task(listener.run_forever())
            shutdown_task = asyncio.create_task(shutdown_event.wait())

            done, pending = await asyncio.wait(
                {listener_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Check if listener died with an error
            for task in done:
                if task is listener_task and task.exception() is not None:
                    raise task.exception()

            # If shutdown was requested, break out
            if shutdown_event.is_set():
                for task in pending:
                    task.cancel()
                break

            # Listener finished without shutdown — treat as disconnect
            for task in pending:
                task.cancel()

        except Exception:
            if shutdown_event.is_set():
                break
            log.exception(
                "Telegram connection lost — reconnecting in %d seconds …",
                backoff,
            )

        # Stop the current listener gracefully
        try:
            await listener.stop()
        except Exception:
            pass

        if shutdown_event.is_set():
            break

        # Wait with backoff (but respect shutdown during the wait)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=backoff)
            break  # shutdown was requested during the wait
        except asyncio.TimeoutError:
            pass  # timeout expired, proceed to reconnect

        backoff = min(backoff * 2, max_backoff)
        log.info("Attempting to reconnect …")

    # ── Cleanup ──
    try:
        await listener.stop()
    except Exception:
        pass
    await broker.shutdown()
    await db.close()


async def _run_backtest(days: int, limit: int | None) -> None:
    """Run the backtest engine and print the report."""
    from config.settings import get_settings
    from backtest.engine import BacktestEngine
    from backtest.report import print_backtest_report
    from storage.db import Database

    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()

    engine = BacktestEngine(settings, db)
    summary = await engine.run(days=days, limit=limit)

    print_backtest_report(summary)

    await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="automa",
        description="Telegram trade signal executor with Groww API",
    )
    parser.add_argument(
        "--mode",
        choices=["live", "paper", "backtest"],
        default=None,
        help="Operating mode (overrides MODE in .env)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days of history for backtest (default: 30)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max messages to process in backtest",
    )
    args = parser.parse_args()

    # Resolve mode: CLI flag > .env > default (paper)
    from config.settings import get_settings
    settings = get_settings()
    mode = args.mode or settings.mode.value

    _setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    logger.info("Automa starting in [bold]%s[/bold] mode", mode.upper())

    if mode == "backtest":
        asyncio.run(_run_backtest(days=args.days, limit=args.limit))
    else:
        asyncio.run(_run_live_or_paper(mode))


if __name__ == "__main__":
    main()
