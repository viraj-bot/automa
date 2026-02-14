#!/usr/bin/env python3
"""Debug script: fetch recent Telegram messages and show parser results.

Run on the cloud VM:
    python debug_parser.py 2>/dev/null

Saves detailed output to data/debug_parser_output.txt.
Share that file to diagnose parser issues.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from parser.signal_parser import (
    SignalParser,
    _clean_text,
    _extract_targets,
    _extract_stoploss,
)
from parser.models import EntrySignal, ExitSignal, BookProfitSignal


async def main() -> None:
    from config.settings import get_settings
    from telegram.history import fetch_chat_history

    settings = get_settings()
    messages = await fetch_chat_history(settings, days=30, limit=200)

    Path("data").mkdir(exist_ok=True)
    outfile = Path("data/debug_parser_output.txt")

    parser = SignalParser()
    lines: list[str] = []
    entry_count = 0
    with_targets = 0
    with_sl = 0

    for msg in messages:
        text = msg["text"]
        signal = parser.parse(text, message_id=msg["id"], timestamp=msg["date"])

        if signal is None:
            continue

        if isinstance(signal, EntrySignal):
            entry_count += 1
            has_t = bool(signal.targets)
            has_sl = signal.stoploss is not None
            if has_t:
                with_targets += 1
            if has_sl:
                with_sl += 1

            lines.append(f"=== ENTRY #{entry_count}: {signal.display_name} @ ₹{signal.entry_price} ===")
            lines.append(f"Targets: {signal.targets}")
            lines.append(f"Stoploss: {signal.stoploss}")
            lines.append(f"RAW:")
            lines.append(text[:600])
            lines.append(f"---CLEANED:")
            lines.append(_clean_text(text)[:600])
            lines.append("")

    summary = (
        f"Total entries: {entry_count}\n"
        f"With targets: {with_targets}\n"
        f"With stoploss: {with_sl}\n"
        f"Without targets: {entry_count - with_targets}\n"
        f"Without stoploss: {entry_count - with_sl}\n"
    )
    lines.insert(0, summary + "\n")

    outfile.write_text("\n".join(lines), encoding="utf-8")
    print(summary)
    print(f"Details saved to {outfile}")
    # Also print first 5 entries to stdout
    for line in lines[2:]:  # skip summary
        print(line)
        if line.startswith("=== ENTRY #5"):
            break


if __name__ == "__main__":
    asyncio.run(main())
