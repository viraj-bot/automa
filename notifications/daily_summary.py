"""Daily P&L summary generator and email sender.

Generates an HTML email summarising the day's trading activity and sends it
via SMTP.  Designed to be called once per market day around 3:30 PM IST.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from config.settings import Settings
from storage.db import Database

logger = logging.getLogger(__name__)

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))


async def generate_and_send_daily_summary(
    settings: Settings,
    db: Database,
) -> None:
    """Gather today's data from the DB, build an HTML email, and send it."""
    if not settings.daily_summary_enabled:
        return

    if not settings.summary_email_to:
        logger.warning("Daily summary enabled but SUMMARY_EMAIL_TO is empty — skipping")
        return

    if not settings.smtp_user or not settings.smtp_password:
        logger.warning("Daily summary enabled but SMTP credentials are empty — skipping")
        return

    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    today_display = datetime.now(IST).strftime("%A, %d %B %Y")

    logger.info("Generating daily summary for %s …", today_str)

    try:
        # Gather data
        today_closed = await db.get_today_closed_positions(today_str)
        today_opened = await db.get_today_opened_positions(today_str)
        open_positions = await db.get_all_positions(status="OPEN")
        today_summary = await db.get_today_trade_summary(today_str)
        overall_summary = await db.get_overall_trade_summary()
        signal_counts = await db.get_signals_count_for_date(today_str)

        html = _build_html(
            date_display=today_display,
            today_closed=today_closed,
            today_opened=today_opened,
            open_positions=open_positions,
            today_summary=today_summary,
            overall_summary=overall_summary,
            signal_counts=signal_counts,
            mode=settings.mode.value.upper(),
        )

        subject = _build_subject(today_display, today_summary)

        # Send email in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _send_email,
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_user,
            settings.smtp_password,
            settings.summary_email_to,
            subject,
            html,
        )

        logger.info("Daily summary email sent to %s", settings.summary_email_to)

    except Exception:
        logger.exception("Failed to generate/send daily summary")


def _build_subject(date_display: str, summary: dict[str, Any]) -> str:
    total_pnl = summary.get("total_pnl", 0) or 0
    total_trades = summary.get("total_trades", 0) or 0
    sign = "+" if total_pnl >= 0 else ""
    if total_trades == 0:
        return f"Automa Daily Summary — {date_display} — No trades"
    return f"Automa Daily Summary — {date_display} — {sign}₹{total_pnl:,.2f}"


def _fmt_pnl(pnl: float) -> str:
    """Format P&L with colour and sign."""
    if pnl > 0:
        return f'<span style="color:#16a34a;font-weight:bold">+₹{pnl:,.2f}</span>'
    elif pnl < 0:
        return f'<span style="color:#dc2626;font-weight:bold">-₹{abs(pnl):,.2f}</span>'
    return '<span style="color:#6b7280">₹0.00</span>'


def _build_html(
    date_display: str,
    today_closed: list[dict[str, Any]],
    today_opened: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    today_summary: dict[str, Any],
    overall_summary: dict[str, Any],
    signal_counts: dict[str, int],
    mode: str,
) -> str:
    """Build a clean, mobile-friendly HTML email."""

    total_pnl = today_summary.get("total_pnl", 0) or 0
    wins = today_summary.get("wins", 0) or 0
    losses = today_summary.get("losses", 0) or 0
    total_trades = today_summary.get("total_trades", 0) or 0
    best = today_summary.get("best_trade", 0) or 0
    worst = today_summary.get("worst_trade", 0) or 0
    avg_pnl = today_summary.get("avg_pnl", 0) or 0
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    overall_pnl = overall_summary.get("total_pnl", 0) or 0
    overall_trades = overall_summary.get("total_trades", 0) or 0
    overall_wins = overall_summary.get("wins", 0) or 0
    overall_win_rate = (overall_wins / overall_trades * 100) if overall_trades > 0 else 0

    sig_total = signal_counts.get("total", 0) or 0
    sig_entries = signal_counts.get("entries", 0) or 0
    sig_exits = signal_counts.get("exits", 0) or 0
    sig_bp = signal_counts.get("book_profits", 0) or 0

    # ── Styles ──
    css = """
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #f8fafc; margin: 0; padding: 20px; color: #1e293b; }
        .container { max-width: 640px; margin: 0 auto; background: #ffffff;
                     border-radius: 12px; overflow: hidden;
                     box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .header { background: linear-gradient(135deg, #1e40af, #3b82f6);
                  color: white; padding: 24px 28px; }
        .header h1 { margin: 0; font-size: 22px; font-weight: 600; }
        .header p { margin: 6px 0 0; opacity: 0.85; font-size: 14px; }
        .mode-badge { display: inline-block; background: rgba(255,255,255,0.2);
                      padding: 2px 10px; border-radius: 12px; font-size: 11px;
                      font-weight: 600; letter-spacing: 0.5px; margin-left: 8px; }
        .content { padding: 24px 28px; }
        .pnl-hero { text-align: center; padding: 20px 0; margin-bottom: 20px;
                    border-bottom: 1px solid #e2e8f0; }
        .pnl-hero .label { font-size: 13px; color: #64748b; text-transform: uppercase;
                           letter-spacing: 1px; margin-bottom: 4px; }
        .pnl-hero .amount { font-size: 36px; font-weight: 700; }
        .pnl-positive { color: #16a34a; }
        .pnl-negative { color: #dc2626; }
        .pnl-zero { color: #6b7280; }
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px;
                      margin-bottom: 24px; }
        .stat-card { background: #f8fafc; border-radius: 8px; padding: 14px;
                     text-align: center; }
        .stat-card .value { font-size: 22px; font-weight: 700; color: #1e293b; }
        .stat-card .label { font-size: 11px; color: #64748b; text-transform: uppercase;
                            letter-spacing: 0.5px; margin-top: 2px; }
        h2 { font-size: 16px; font-weight: 600; color: #334155;
             margin: 24px 0 12px; padding-bottom: 8px; border-bottom: 2px solid #e2e8f0; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { background: #f1f5f9; color: #475569; font-weight: 600; text-align: left;
             padding: 8px 10px; font-size: 11px; text-transform: uppercase;
             letter-spacing: 0.5px; }
        td { padding: 8px 10px; border-bottom: 1px solid #f1f5f9; }
        tr:last-child td { border-bottom: none; }
        .text-right { text-align: right; }
        .text-center { text-align: center; }
        .footer { background: #f8fafc; padding: 16px 28px; text-align: center;
                  font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }
        .no-trades { text-align: center; padding: 30px; color: #94a3b8; font-size: 14px; }
    </style>
    """

    # ── P&L hero ──
    if total_pnl > 0:
        pnl_class = "pnl-positive"
        pnl_display = f"+₹{total_pnl:,.2f}"
    elif total_pnl < 0:
        pnl_class = "pnl-negative"
        pnl_display = f"-₹{abs(total_pnl):,.2f}"
    else:
        pnl_class = "pnl-zero"
        pnl_display = "₹0.00"

    # ── Today's closed trades table ──
    if today_closed:
        trade_rows = ""
        for i, pos in enumerate(today_closed, 1):
            pnl_val = pos.get("pnl", 0) or 0
            trade_rows += f"""
            <tr>
                <td class="text-center">{i}</td>
                <td>{pos.get('trading_symbol', '—')}</td>
                <td class="text-center">{pos.get('option_type', '—')}</td>
                <td class="text-right">{pos.get('quantity', 0)}</td>
                <td class="text-right">₹{pos.get('avg_entry_price', 0):,.2f}</td>
                <td class="text-right">{_fmt_pnl(pnl_val)}</td>
            </tr>"""

        trades_section = f"""
        <h2>Today's Closed Trades ({len(today_closed)})</h2>
        <table>
            <tr>
                <th class="text-center">#</th>
                <th>Instrument</th>
                <th class="text-center">Type</th>
                <th class="text-right">Qty</th>
                <th class="text-right">Entry ₹</th>
                <th class="text-right">P&L</th>
            </tr>
            {trade_rows}
        </table>"""
    else:
        trades_section = '<div class="no-trades">No trades closed today</div>'

    # ── Open positions table ──
    if open_positions:
        open_rows = ""
        for i, pos in enumerate(open_positions, 1):
            open_rows += f"""
            <tr>
                <td class="text-center">{i}</td>
                <td>{pos.get('trading_symbol', '—')}</td>
                <td class="text-center">{pos.get('option_type', '—')}</td>
                <td class="text-right">{pos.get('quantity', 0)}</td>
                <td class="text-right">₹{pos.get('avg_entry_price', 0):,.2f}</td>
                <td class="text-right">₹{pos.get('stoploss', 0) or 0:,.2f}</td>
            </tr>"""

        open_section = f"""
        <h2>Open Positions ({len(open_positions)})</h2>
        <table>
            <tr>
                <th class="text-center">#</th>
                <th>Instrument</th>
                <th class="text-center">Type</th>
                <th class="text-right">Qty</th>
                <th class="text-right">Entry ₹</th>
                <th class="text-right">SL ₹</th>
            </tr>
            {open_rows}
        </table>"""
    else:
        open_section = ""

    # ── Signal activity ──
    signal_section = f"""
    <h2>Signal Activity</h2>
    <table>
        <tr><td>Total signals received</td><td class="text-right"><b>{sig_total}</b></td></tr>
        <tr><td>Entry signals</td><td class="text-right">{sig_entries}</td></tr>
        <tr><td>Exit signals</td><td class="text-right">{sig_exits}</td></tr>
        <tr><td>Book profit signals</td><td class="text-right">{sig_bp}</td></tr>
    </table>"""

    # ── Overall performance ──
    overall_section = f"""
    <h2>Overall Performance (Lifetime)</h2>
    <table>
        <tr><td>Total trades</td><td class="text-right"><b>{overall_trades}</b></td></tr>
        <tr><td>Win rate</td><td class="text-right">{overall_win_rate:.1f}%</td></tr>
        <tr><td>Total P&L</td><td class="text-right">{_fmt_pnl(overall_pnl)}</td></tr>
    </table>"""

    # ── Assemble ──
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{css}
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Automa Daily Summary <span class="mode-badge">{mode}</span></h1>
        <p>{date_display}</p>
    </div>
    <div class="content">
        <div class="pnl-hero">
            <div class="label">Today's P&L</div>
            <div class="amount {pnl_class}">{pnl_display}</div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="value">{total_trades}</div>
                <div class="label">Trades</div>
            </div>
            <div class="stat-card">
                <div class="value" style="color:#16a34a">{wins}</div>
                <div class="label">Wins</div>
            </div>
            <div class="stat-card">
                <div class="value" style="color:#dc2626">{losses}</div>
                <div class="label">Losses</div>
            </div>
        </div>

        <table style="margin-bottom:20px">
            <tr><td>Win Rate</td><td class="text-right"><b>{win_rate:.1f}%</b></td></tr>
            <tr><td>Avg P&L per trade</td><td class="text-right">{_fmt_pnl(avg_pnl)}</td></tr>
            <tr><td>Best trade</td><td class="text-right">{_fmt_pnl(best)}</td></tr>
            <tr><td>Worst trade</td><td class="text-right">{_fmt_pnl(worst)}</td></tr>
        </table>

        {trades_section}
        {open_section}
        {signal_section}
        {overall_section}
    </div>
    <div class="footer">
        Generated by Automa at {datetime.now(IST).strftime('%I:%M %p IST')}
    </div>
</div>
</body>
</html>"""

    return html


def _send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    to_addr: str,
    subject: str,
    html_body: str,
) -> None:
    """Send an HTML email via SMTP (blocking — run in executor)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr

    # Plain-text fallback
    plain = (
        f"{subject}\n\n"
        "This email is best viewed in an HTML-capable email client.\n"
        "Please enable HTML to see the full daily summary."
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [to_addr], msg.as_string())
