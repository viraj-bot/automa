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
    overview.add_row("Duplicates skipped", str(summary.get("skipped_duplicates", 0)))
    console.print(overview)
    console.print()

    # ── Trade statistics ─────────────────────────────────────────────────
    total = summary.get("total_trades", 0)
    if total == 0:
        console.print(
            Panel("[yellow]No closed trades to report.[/yellow]", expand=False)
        )
        return

    wins = summary.get("wins", 0)
    losses = summary.get("losses", 0)
    win_rate = (wins / total * 100) if total else 0

    stats = Table(title="Trade Statistics", show_header=False, box=None, padding=(0, 2))
    stats.add_column("Metric", style="bold")
    stats.add_column("Value", justify="right")

    stats.add_row("Total closed trades", str(total))
    stats.add_row("Winning trades", f"[green]{wins}[/green]")
    stats.add_row("Losing trades", f"[red]{losses}[/red]")
    stats.add_row("Win rate", f"{win_rate:.1f}%")
    console.print(stats)
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
