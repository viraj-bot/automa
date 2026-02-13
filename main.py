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
import signal
import sys

from rich.logging import RichHandler


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
    """Start the Telegram listener with either the live or paper broker."""
    from config.settings import AppMode, get_settings
    from broker.paper_broker import PaperBroker
    from broker.groww_broker import GrowwBroker
    from parser.signal_parser import SignalParser
    from storage.db import Database
    from telegram.listener import TelegramListener

    settings = get_settings()
    # Override mode if passed via CLI
    settings_mode = AppMode(mode)

    db = Database(settings.database_path)
    await db.connect()

    parser = SignalParser()

    if settings_mode == AppMode.LIVE:
        broker = GrowwBroker(settings, db)
    else:
        broker = PaperBroker(settings, db)

    await broker.initialize()

    listener = TelegramListener(
        settings=settings,
        parser=parser,
        broker=broker,
        db=db,
    )

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal() -> None:
        logging.getLogger(__name__).info("Shutdown signal received …")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    await listener.start()

    # Run until shutdown
    listener_task = asyncio.create_task(listener.run_forever())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        {listener_task, shutdown_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cleanup
    for task in pending:
        task.cancel()

    await listener.stop()
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
