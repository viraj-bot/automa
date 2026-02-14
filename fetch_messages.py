"""Fetch the last 30 days of Telegram messages and save to a log file.

Run on the cloud VM (where Telegram is accessible):
    python fetch_messages.py

Output: data/telegram_messages.log
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Auto-detect custom CA bundle
_ca_bundle = Path("data/ca-bundle.pem")
if _ca_bundle.exists() and _ca_bundle.stat().st_size > 0:
    _bundle = str(_ca_bundle.resolve())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)
    os.environ.setdefault("SSL_CERT_FILE", _bundle)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_messages")

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from config.settings import get_settings
from telegram.history import fetch_chat_history


async def main() -> None:
    settings = get_settings()
    days = 30

    logger.info("Fetching last %d days of messages from Telegram group %s ...",
                days, settings.telegram_group_id)

    messages = await fetch_chat_history(settings, days=days)

    if not messages:
        logger.warning("No messages fetched!")
        return

    # Write to log file
    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "telegram_messages.log"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Telegram messages from group {settings.telegram_group_id}\n")
        f.write(f"# Last {days} days — {len(messages)} messages\n")
        f.write(f"# Format: [msg_id] [date] [text]\n")
        f.write("=" * 80 + "\n\n")

        for msg in messages:
            msg_id = msg["id"]
            date = msg["date"]
            text = msg["text"]

            if hasattr(date, "strftime"):
                date_str = date.strftime("%Y-%m-%d %H:%M:%S")
            else:
                date_str = str(date)

            f.write(f"=== MSG #{msg_id} | {date_str} ===\n")
            f.write(text)
            f.write("\n\n")

    logger.info("Saved %d messages to %s", len(messages), out_path)
    print(f"\nDone! {len(messages)} messages saved to {out_path}")
    print(f"File size: {out_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    asyncio.run(main())
