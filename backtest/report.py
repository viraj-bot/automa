"""Pretty-print backtest results using Rich tables."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


def print_backtest_report(summary: dict[str, Any]) -> None:
    """Render a nicely formatted backtest report to the terminal."""
    console = Console()

    console.print()
    console.rule("[bold blue]Backtest Report[/bold blue]")
    console.print()

    # ── Overview table ───────────────────────────────────────────────────
    overview = Table(title="Overview", show_header=False, box=None, padding=(0, 2))
    overview.add_column("Metric", style="bold")
    overview.add_column("Value", justify="right")

    overview.add_row("Messages analysed", str(summary.get("total_messages", 0)))
    overview.add_row("Signals parsed", str(summary.get("total_signals", 0)))

    entries = summary.get("entries", 0)
    exits = summary.get("exits", 0)
    bps = summary.get("book_profits", 0)
    entries_w = summary.get("entries_with_targets", 0)
    entries_wo = summary.get("entries_without_targets", 0)
    bp_w = summary.get("bp_with_price", 0)
    bp_wo = summary.get("bp_without_price", 0)

    overview.add_row("  Entry signals", f"[green]{entries}[/green]")
    if entries_w or entries_wo:
        overview.add_row("    with targets", f"[green]{entries_w}[/green]")
        overview.add_row("    without targets", f"[dim]{entries_wo}[/dim]")
    overview.add_row("  Exit signals", f"[yellow]{exits}[/yellow]")
    overview.add_row("  Book profit signals", f"[cyan]{bps}[/cyan]")
    if bp_w or bp_wo:
        overview.add_row("    with exit price", f"[green]{bp_w}[/green]")
        overview.add_row("    without exit price", f"[dim]{bp_wo}[/dim]")

    unparsed = summary.get("unparsed_messages", 0)
    if unparsed:
        overview.add_row("  Unparsed messages", f"[dim]{unparsed}[/dim]")

    errors = summary.get("errors", 0)
    if errors:
        overview.add_row("  Errors", f"[red]{errors}[/red]")

    console.print(overview)
    console.print()

    # ── Per-trade table ───────────────────────────────────────────────────
    trade_log = summary.get("trade_log", [])
    total = summary.get("total_trades", 0)

    if total == 0:
        console.print(
            Panel("[yellow]No closed trades to report.[/yellow]", expand=False)
        )
        return

    trade_table = Table(title="Trade Log", show_lines=True)
    trade_table.add_column("#", justify="right", style="dim", width=3)
    trade_table.add_column("Instrument", style="bold", min_width=18)
    trade_table.add_column("Qty", justify="right", width=5)
    trade_table.add_column("Entry Date", width=12)
    trade_table.add_column("Entry ₹", justify="right", width=9)
    trade_table.add_column("Exit Date", width=12)
    trade_table.add_column("Exit ₹", justify="right", width=9)
    trade_table.add_column("Exit Src", width=9)
    trade_table.add_column("Type", width=7)
    trade_table.add_column("P&L ₹", justify="right", width=12)

    for t in trade_log:
        pnl = t["pnl"]
        pnl_colour = "green" if pnl > 0 else ("red" if pnl < 0 else "dim")
        exit_src = t["exit_source"]
        src_colour = {
            "groww": "green",
            "signal": "green",
            "target": "yellow",
            "stoploss": "red",
            "entry": "dim",
        }.get(exit_src, "white")

        # Shorten close type for display
        close_short = {
            "BOOK_PROFIT": "BP",
            "EXIT": "EXIT",
            "ORPHAN": "ORPHAN",
            "PARTIAL": "PARTIAL",
        }.get(t["close_type"], t["close_type"])

        entry_time = t.get("entry_time", "")
        exit_time = t.get("exit_time", "")

        trade_table.add_row(
            str(t["trade_no"]),
            t["instrument"],
            str(t["qty"]),
            f"[dim]{entry_time}[/dim]",
            f"{t['entry_price']:,.2f}",
            f"[dim]{exit_time}[/dim]",
            f"{t['exit_price']:,.2f}",
            f"[{src_colour}]{exit_src}[/{src_colour}]",
            close_short,
            f"[{pnl_colour}]{pnl:,.2f}[/{pnl_colour}]",
        )

    console.print(trade_table)
    console.print()

    # ── Trade statistics ─────────────────────────────────────────────────
    wins = summary.get("wins", 0) or 0
    losses = summary.get("losses", 0) or 0
    breakeven = summary.get("breakeven", 0) or 0
    partial_count = sum(1 for t in trade_log if t["close_type"] == "PARTIAL")
    win_rate = (wins / total * 100) if total else 0

    stats = Table(title="Trade Statistics", show_header=False, box=None, padding=(0, 2))
    stats.add_column("Metric", style="bold")
    stats.add_column("Value", justify="right")

    stats.add_row("Total closed trades", str(total))
    stats.add_row("Winning trades", f"[green]{wins}[/green]")
    stats.add_row("Losing trades", f"[red]{losses}[/red]")
    if breakeven > 0:
        stats.add_row("Break-even trades (P&L = 0)", f"[dim]{breakeven}[/dim]")
    if partial_count > 0:
        stats.add_row("Partial exits", f"[cyan]{partial_count}[/cyan]")
    stats.add_row("Win rate", f"{win_rate:.1f}%")

    if wins > 0 and losses > 0:
        best = summary.get("best_trade", 0) or 0
        worst = abs(summary.get("worst_trade", 0) or 0)
        if worst > 0:
            stats.add_row("Risk/Reward (best/worst)", f"{best / worst:.2f}")

    console.print(stats)
    console.print()

    # ── Price source breakdown ────────────────────────────────────────
    diag = Table(
        title="Price Sources", show_header=False, box=None, padding=(0, 2)
    )
    diag.add_column("Metric", style="bold")
    diag.add_column("Value", justify="right")

    entry_groww = summary.get("entry_from_groww", 0)
    entry_signal = summary.get("entry_from_signal", 0)
    diag.add_row("Entries at Groww market price", f"[green]{entry_groww}[/green]")
    diag.add_row("Entries at signal price", f"[yellow]{entry_signal}[/yellow]")

    exit_groww = summary.get("exit_from_groww", 0)
    exit_signal = summary.get("exit_from_signal", 0)
    exit_target = summary.get("exit_from_target", 0)
    exit_stoploss = summary.get("exit_from_stoploss", 0)
    exit_entry = summary.get("exit_from_entry", 0)

    diag.add_row("Exits at Groww market price", f"[green]{exit_groww}[/green]")
    diag.add_row("Exits at signal price (book profit)", f"[green]{exit_signal}[/green]")
    diag.add_row("Exits at target (estimated)", f"[yellow]{exit_target}[/yellow]")
    diag.add_row("Exits at stoploss (orphan/worst case)", f"[red]{exit_stoploss}[/red]")
    if exit_entry:
        diag.add_row("Exits at entry (no data)", f"[dim]{exit_entry}[/dim]")

    unmatched = summary.get("unmatched_close_signals", 0)
    if unmatched:
        unmatched_exit = summary.get("unmatched_exit_signals", 0)
        unmatched_bp = summary.get("unmatched_bp_signals", 0)
        diag.add_row("", "")
        diag.add_row(
            "Unmatched close signals",
            f"[dim]{unmatched}[/dim]",
        )
        if unmatched_exit:
            diag.add_row(
                "  - Exit (position already closed)",
                f"[dim]{unmatched_exit}[/dim]",
            )
        if unmatched_bp:
            diag.add_row(
                "  - Book profit (P&L may be lost)",
                f"[yellow]{unmatched_bp}[/yellow]",
            )

    console.print(diag)
    console.print()

    # ── P&L table ────────────────────────────────────────────────────────
    pnl_table = Table(title="Profit & Loss", show_header=False, box=None, padding=(0, 2))
    pnl_table.add_column("Metric", style="bold")
    pnl_table.add_column("Value", justify="right")

    total_pnl = summary.get("total_pnl", 0) or 0
    avg_pnl = summary.get("avg_pnl", 0) or 0
    best = summary.get("best_trade", 0) or 0
    worst = summary.get("worst_trade", 0) or 0

    pnl_colour = "green" if total_pnl >= 0 else "red"
    pnl_table.add_row("Total P&L", f"[{pnl_colour}]₹{total_pnl:,.2f}[/{pnl_colour}]")
    pnl_table.add_row("Average P&L per trade", f"₹{avg_pnl:,.2f}")
    pnl_table.add_row("Best trade", f"[green]₹{best:,.2f}[/green]")
    pnl_table.add_row("Worst trade", f"[red]₹{worst:,.2f}[/red]")

    console.print(pnl_table)
    console.print()
    console.rule()
