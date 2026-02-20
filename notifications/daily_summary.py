"""Daily P&L summary generator and email sender.

Generates an HTML email summarising the day's trading activity and sends it
via SMTP.  Designed to be called once per market day around 3:30 PM IST.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Any

from config.settings import Settings
from storage.db import Database

logger = logging.getLogger(__name__)

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))


async def _fetch_groww_live_data(broker) -> dict[str, Any]:
    """Fetch today's orders and positions from the Groww API.

    Returns a dict with keys ``groww_orders`` and ``groww_positions``.
    On any failure the corresponding list is empty (never blocks the email).
    """
    result: dict[str, Any] = {"groww_orders": [], "groww_positions": []}
    if broker is None:
        return result

    try:
        from broker.groww_broker import GrowwBroker
        if not isinstance(broker, GrowwBroker):
            return result
    except ImportError:
        return result

    loop = asyncio.get_running_loop()

    try:
        orders_resp = await broker._call_groww_api(
            lambda: broker.groww.get_order_list(segment="FNO", page=0, page_size=50),
        )
        result["groww_orders"] = orders_resp.get("orders", orders_resp) if isinstance(orders_resp, dict) else orders_resp
    except Exception:
        logger.debug("Could not fetch Groww order list for summary", exc_info=True)

    try:
        positions_resp = await broker._call_groww_api(
            lambda: broker.groww.get_positions_for_user(segment="FNO"),
        )
        result["groww_positions"] = positions_resp.get("positions", positions_resp) if isinstance(positions_resp, dict) else positions_resp
    except Exception:
        logger.debug("Could not fetch Groww positions for summary", exc_info=True)

    try:
        margin_resp = await broker._call_groww_api(
            lambda: broker.groww.get_available_margin_details(),
        )
        result["groww_margin"] = margin_resp
    except Exception:
        logger.debug("Could not fetch Groww margin for summary", exc_info=True)

    return result


async def generate_and_send_daily_summary(
    settings: Settings,
    db: Database,
    broker=None,
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
        # Gather data from DB
        today_closed = await db.get_today_closed_positions(today_str)
        today_opened = await db.get_today_opened_positions(today_str)
        open_positions = await db.get_all_positions(status="OPEN")
        today_summary = await db.get_today_trade_summary(today_str)
        overall_summary = await db.get_overall_trade_summary()
        signal_counts = await db.get_signals_count_for_date(today_str)

        # Fetch live data from Groww API (best-effort)
        groww_data = await _fetch_groww_live_data(broker)

        html = _build_html(
            date_display=today_display,
            today_closed=today_closed,
            today_opened=today_opened,
            open_positions=open_positions,
            today_summary=today_summary,
            overall_summary=overall_summary,
            signal_counts=signal_counts,
            mode=settings.mode.value.upper(),
            groww_data=groww_data,
        )

        subject = _build_subject(today_display, today_summary)

        # Locate today's daily message log file for the current mode
        from storage.daily_log import get_daily_log_path
        mode_str = settings.mode.value
        daily_log = get_daily_log_path(mode=mode_str, date_str=today_str)
        attachment = str(daily_log) if daily_log else None

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
            attachment,
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
    groww_data: dict[str, Any] | None = None,
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

    # ── Mode-specific banner colours ──
    # LIVE = red, PAPER = yellow/amber, BACKTEST = green
    _MODE_GRADIENTS = {
        "LIVE": "linear-gradient(135deg, #b91c1c, #ef4444)",
        "PAPER": "linear-gradient(135deg, #b45309, #f59e0b)",
        "BACKTEST": "linear-gradient(135deg, #15803d, #22c55e)",
    }
    header_gradient = _MODE_GRADIENTS.get(mode, "linear-gradient(135deg, #1e40af, #3b82f6)")

    # ── Styles ──
    css = f"""
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #f8fafc; margin: 0; padding: 20px; color: #1e293b; }}
        .container {{ max-width: 640px; margin: 0 auto; background: #ffffff;
                     border-radius: 12px; overflow: hidden;
                     box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .header {{ background: {header_gradient};
                  color: white; padding: 24px 28px; }}
        .header h1 {{ margin: 0; font-size: 22px; font-weight: 600; }}
        .header p {{ margin: 6px 0 0; opacity: 0.85; font-size: 14px; }}
        .mode-badge {{ display: inline-block; background: rgba(255,255,255,0.2);
                      padding: 2px 10px; border-radius: 12px; font-size: 11px;
                      font-weight: 600; letter-spacing: 0.5px; margin-left: 8px; }}
        .content {{ padding: 24px 28px; }}
        .pnl-hero {{ text-align: center; padding: 20px 0; margin-bottom: 20px;
                    border-bottom: 1px solid #e2e8f0; }}
        .pnl-hero .label {{ font-size: 13px; color: #64748b; text-transform: uppercase;
                           letter-spacing: 1px; margin-bottom: 4px; }}
        .pnl-hero .amount {{ font-size: 36px; font-weight: 700; }}
        .pnl-positive {{ color: #16a34a; }}
        .pnl-negative {{ color: #dc2626; }}
        .pnl-zero {{ color: #6b7280; }}
        .stats-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px;
                      margin-bottom: 24px; }}
        .stat-card {{ background: #f8fafc; border-radius: 8px; padding: 14px;
                     text-align: center; }}
        .stat-card .value {{ font-size: 22px; font-weight: 700; color: #1e293b; }}
        .stat-card .label {{ font-size: 11px; color: #64748b; text-transform: uppercase;
                            letter-spacing: 0.5px; margin-top: 2px; }}
        h2 {{ font-size: 16px; font-weight: 600; color: #334155;
             margin: 24px 0 12px; padding-bottom: 8px; border-bottom: 2px solid #e2e8f0; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th {{ background: #f1f5f9; color: #475569; font-weight: 600; text-align: left;
             padding: 8px 10px; font-size: 11px; text-transform: uppercase;
             letter-spacing: 0.5px; }}
        td {{ padding: 8px 10px; border-bottom: 1px solid #f1f5f9; }}
        tr:last-child td {{ border-bottom: none; }}
        .text-right {{ text-align: right; }}
        .text-center {{ text-align: center; }}
        .footer {{ background: #f8fafc; padding: 16px 28px; text-align: center;
                  font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
        .no-trades {{ text-align: center; padding: 30px; color: #94a3b8; font-size: 14px; }}
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

    # ── Groww live data sections (LIVE mode only) ──
    groww_orders_section = ""
    groww_positions_section = ""
    groww_margin_section = ""

    if groww_data and mode == "LIVE":
        # Groww orders
        groww_orders = groww_data.get("groww_orders", [])
        if isinstance(groww_orders, list) and groww_orders:
            order_rows = ""
            for o in groww_orders:
                status = o.get("order_status", "—")
                status_color = {
                    "EXECUTED": "#16a34a", "REJECTED": "#dc2626",
                    "CANCELLED": "#dc2626", "FAILED": "#dc2626",
                }.get(status, "#64748b")
                price_val = o.get("avg_fill_price") or o.get("price") or 0
                try:
                    price_str = f"₹{float(price_val):,.2f}"
                except (ValueError, TypeError):
                    price_str = str(price_val)
                order_rows += f"""
                <tr>
                    <td>{o.get('trading_symbol', '—')}</td>
                    <td class="text-center">{o.get('transaction_type', '—')}</td>
                    <td class="text-center">{o.get('order_type', '—')}</td>
                    <td class="text-right">{o.get('quantity', 0)}</td>
                    <td class="text-right">{price_str}</td>
                    <td class="text-center" style="color:{status_color};font-weight:600">{status}</td>
                    <td style="font-size:11px">{o.get('groww_order_id', '—')[:16]}</td>
                </tr>"""

            groww_orders_section = f"""
            <h2>Groww Orders — Today ({len(groww_orders)})</h2>
            <div style="overflow-x:auto">
            <table>
                <tr>
                    <th>Symbol</th>
                    <th class="text-center">Side</th>
                    <th class="text-center">Type</th>
                    <th class="text-right">Qty</th>
                    <th class="text-right">Price</th>
                    <th class="text-center">Status</th>
                    <th>Order ID</th>
                </tr>
                {order_rows}
            </table>
            </div>"""

        # Groww positions
        groww_positions = groww_data.get("groww_positions", [])
        if isinstance(groww_positions, list) and groww_positions:
            pos_rows = ""
            for p in groww_positions:
                net_qty = p.get("net_quantity", p.get("quantity", 0))
                buy_val = p.get("buy_value", 0) or 0
                sell_val = p.get("sell_value", 0) or 0
                try:
                    unrealised = float(sell_val) - float(buy_val)
                except (ValueError, TypeError):
                    unrealised = 0
                avg_price = p.get("average_price", p.get("buy_avg", 0))
                try:
                    avg_str = f"₹{float(avg_price):,.2f}"
                except (ValueError, TypeError):
                    avg_str = str(avg_price)
                ltp = p.get("ltp", p.get("last_traded_price", "—"))
                try:
                    ltp_str = f"₹{float(ltp):,.2f}"
                except (ValueError, TypeError):
                    ltp_str = str(ltp)
                pos_rows += f"""
                <tr>
                    <td>{p.get('trading_symbol', '—')}</td>
                    <td class="text-right">{net_qty}</td>
                    <td class="text-right">{avg_str}</td>
                    <td class="text-right">{ltp_str}</td>
                    <td class="text-right">{_fmt_pnl(unrealised)}</td>
                </tr>"""

            groww_positions_section = f"""
            <h2>Groww Live Positions ({len(groww_positions)})</h2>
            <table>
                <tr>
                    <th>Symbol</th>
                    <th class="text-right">Net Qty</th>
                    <th class="text-right">Avg Price</th>
                    <th class="text-right">LTP</th>
                    <th class="text-right">Unrealised P&L</th>
                </tr>
                {pos_rows}
            </table>"""

        # Groww margin
        groww_margin = groww_data.get("groww_margin")
        if isinstance(groww_margin, dict) and groww_margin:
            avail = groww_margin.get("available_margin", groww_margin.get("net", "—"))
            used = groww_margin.get("used_margin", groww_margin.get("utilised", "—"))
            try:
                avail_str = f"₹{float(avail):,.2f}"
            except (ValueError, TypeError):
                avail_str = str(avail)
            try:
                used_str = f"₹{float(used):,.2f}"
            except (ValueError, TypeError):
                used_str = str(used)
            groww_margin_section = f"""
            <h2>Groww Account</h2>
            <table>
                <tr><td>Available Margin</td><td class="text-right"><b>{avail_str}</b></td></tr>
                <tr><td>Used Margin</td><td class="text-right">{used_str}</td></tr>
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

        {groww_margin_section}
        {trades_section}
        {groww_orders_section}
        {open_section}
        {groww_positions_section}
        {signal_section}
        {overall_section}
    </div>
    <div class="footer">
        Generated by Automa ({mode}) at {datetime.now(IST).strftime('%I:%M %p IST on %d %b %Y')}
    </div>
</div>
</body>
</html>"""

    return html


# ── Backtest summary email ─────────────────────────────────────────────


async def send_backtest_summary_email(
    settings: Settings,
    summary: dict[str, Any],
) -> None:
    """Build an HTML email from the backtest summary dict and send it."""
    if not settings.summary_email_to:
        logger.info("No SUMMARY_EMAIL_TO configured — skipping backtest email")
        return
    if not settings.smtp_user or not settings.smtp_password:
        logger.warning("SMTP credentials not configured — skipping backtest email")
        return

    html = _build_backtest_html(summary)
    total_pnl = summary.get("total_pnl", 0) or 0
    total_trades = summary.get("total_trades", 0) or 0
    days = summary.get("days", "?")
    sign = "+" if total_pnl >= 0 else ""

    if total_trades == 0:
        subject = f"Automa Backtest Report — {days} days — No trades"
    else:
        subject = f"Automa Backtest Report — {days} days — {total_trades} trades — {sign}₹{total_pnl:,.2f}"

    # Locate today's backtest log file (if any)
    from storage.daily_log import get_daily_log_path
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    daily_log = get_daily_log_path(mode="backtest", date_str=today_str)
    attachment = str(daily_log) if daily_log else None

    try:
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
            attachment,
        )
        logger.info("Backtest summary email sent to %s", settings.summary_email_to)
    except Exception:
        logger.exception("Failed to send backtest summary email")


def _build_backtest_html(summary: dict[str, Any]) -> str:
    """Build an HTML email from the backtest engine's summary dict."""
    from datetime import datetime, timezone, timedelta
    IST_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST_tz)

    total_pnl = summary.get("total_pnl", 0) or 0
    wins = summary.get("wins", 0) or 0
    losses = summary.get("losses", 0) or 0
    breakeven = summary.get("breakeven", 0) or 0
    total_trades = summary.get("total_trades", 0) or 0
    best = summary.get("best_trade", 0) or 0
    worst = summary.get("worst_trade", 0) or 0
    avg_pnl = summary.get("avg_pnl", 0) or 0
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    days = summary.get("days", "?")

    entries = summary.get("entries", 0) or 0
    exits = summary.get("exits", 0) or 0
    bps = summary.get("book_profits", 0) or 0
    total_messages = summary.get("total_messages", 0) or 0
    total_signals = summary.get("total_signals", 0) or 0
    unparsed = summary.get("unparsed_messages", 0) or 0

    trade_log = summary.get("trade_log", [])

    # P&L display
    if total_pnl > 0:
        pnl_class = "pnl-positive"
        pnl_display = f"+₹{total_pnl:,.2f}"
    elif total_pnl < 0:
        pnl_class = "pnl-negative"
        pnl_display = f"-₹{abs(total_pnl):,.2f}"
    else:
        pnl_class = "pnl-zero"
        pnl_display = "₹0.00"

    # CSS (reuse same styles)
    css = """
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #f8fafc; margin: 0; padding: 20px; color: #1e293b; }
        .container { max-width: 720px; margin: 0 auto; background: #ffffff;
                     border-radius: 12px; overflow: hidden;
                     box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .header { background: linear-gradient(135deg, #15803d, #22c55e);
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
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
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

    # ── Trade log table ──
    if trade_log:
        trade_rows = ""
        for t in trade_log:
            pnl_val = t.get("pnl", 0) or 0
            entry_time = t.get("entry_time", "—")
            exit_time = t.get("exit_time", "—")
            exit_src = t.get("exit_source", "—")
            close_type = {
                "BOOK_PROFIT": "BP",
                "EXIT": "EXIT",
                "ORPHAN": "ORPHAN",
                "PARTIAL": "PARTIAL",
            }.get(t.get("close_type", ""), t.get("close_type", "—"))

            # Format stoploss
            sl_val = t.get("stoploss")
            sl_str = f'₹{float(sl_val):,.2f}' if sl_val is not None and float(sl_val) > 0 else "—"

            # Format targets
            targets = t.get("targets", [])
            if targets:
                target_str = ", ".join(f"₹{v:,.0f}" for v in targets[:3])
                if len(targets) > 3:
                    target_str += "…"
            else:
                target_str = "—"

            trade_rows += f"""
            <tr>
                <td class="text-center">{t.get('trade_no', '')}</td>
                <td>{t.get('instrument', '—')}</td>
                <td class="text-right">{t.get('qty', 0)}</td>
                <td class="text-center" style="font-size:11px">{entry_time}</td>
                <td class="text-right">₹{t.get('entry_price', 0):,.2f}</td>
                <td class="text-right" style="color:#dc2626">{sl_str}</td>
                <td class="text-right" style="color:#0891b2">{target_str}</td>
                <td class="text-center" style="font-size:11px">{exit_time}</td>
                <td class="text-right">₹{t.get('exit_price', 0):,.2f}</td>
                <td class="text-center">{exit_src}</td>
                <td class="text-center">{close_type}</td>
                <td class="text-right">{_fmt_pnl(pnl_val)}</td>
            </tr>"""

        trades_section = f"""
        <h2>Trade Log ({len(trade_log)} trades)</h2>
        <div style="overflow-x:auto">
        <table>
            <tr>
                <th class="text-center">#</th>
                <th>Instrument</th>
                <th class="text-right">Qty</th>
                <th class="text-center">Entry Date</th>
                <th class="text-right">Entry ₹</th>
                <th class="text-right">SL ₹</th>
                <th class="text-right">Target ₹</th>
                <th class="text-center">Exit Date</th>
                <th class="text-right">Exit ₹</th>
                <th class="text-center">Source</th>
                <th class="text-center">Type</th>
                <th class="text-right">P&L</th>
            </tr>
            {trade_rows}
        </table>
        </div>"""
    else:
        trades_section = '<div class="no-trades">No closed trades in this backtest</div>'

    # ── Price sources ──
    entry_groww = summary.get("entry_from_groww", 0) or 0
    entry_signal = summary.get("entry_from_signal", 0) or 0
    exit_groww = summary.get("exit_from_groww", 0) or 0
    exit_signal = summary.get("exit_from_signal", 0) or 0
    exit_target = summary.get("exit_from_target", 0) or 0
    exit_stoploss = summary.get("exit_from_stoploss", 0) or 0
    exit_entry = summary.get("exit_from_entry", 0) or 0
    unmatched = summary.get("unmatched_close_signals", 0) or 0

    price_section = f"""
    <h2>Price Sources</h2>
    <table>
        <tr><td>Entries at Groww market price</td><td class="text-right">{entry_groww}</td></tr>
        <tr><td>Entries at signal price</td><td class="text-right">{entry_signal}</td></tr>
        <tr><td>Exits at Groww market price</td><td class="text-right">{exit_groww}</td></tr>
        <tr><td>Exits at signal price (book profit)</td><td class="text-right">{exit_signal}</td></tr>
        <tr><td>Exits at target (estimated)</td><td class="text-right">{exit_target}</td></tr>
        <tr><td>Exits at stoploss</td><td class="text-right">{exit_stoploss}</td></tr>
        {"<tr><td>Exits at entry (no data)</td><td class='text-right'>" + str(exit_entry) + "</td></tr>" if exit_entry else ""}
        {"<tr><td>Unmatched close signals</td><td class='text-right'>" + str(unmatched) + "</td></tr>" if unmatched else ""}
    </table>"""

    # ── Overview ──
    overview_section = f"""
    <h2>Overview</h2>
    <table>
        <tr><td>Messages analysed</td><td class="text-right"><b>{total_messages}</b></td></tr>
        <tr><td>Signals parsed</td><td class="text-right">{total_signals}</td></tr>
        <tr><td>&nbsp;&nbsp;Entry signals</td><td class="text-right">{entries}</td></tr>
        <tr><td>&nbsp;&nbsp;Exit signals</td><td class="text-right">{exits}</td></tr>
        <tr><td>&nbsp;&nbsp;Book profit signals</td><td class="text-right">{bps}</td></tr>
        {"<tr><td>&nbsp;&nbsp;Unparsed messages</td><td class='text-right'>" + str(unparsed) + "</td></tr>" if unparsed else ""}
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
        <h1>Automa Backtest Report <span class="mode-badge">BACKTEST</span></h1>
        <p>Last {days} days &mdash; {total_messages} messages analysed</p>
    </div>
    <div class="content">
        <div class="pnl-hero">
            <div class="label">Total P&L</div>
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
            <div class="stat-card">
                <div class="value">{win_rate:.1f}%</div>
                <div class="label">Win Rate</div>
            </div>
        </div>

        <table style="margin-bottom:20px">
            <tr><td>Avg P&L per trade</td><td class="text-right">{_fmt_pnl(avg_pnl)}</td></tr>
            <tr><td>Best trade</td><td class="text-right">{_fmt_pnl(best)}</td></tr>
            <tr><td>Worst trade</td><td class="text-right">{_fmt_pnl(worst)}</td></tr>
            {"<tr><td>Break-even trades</td><td class='text-right'>" + str(breakeven) + "</td></tr>" if breakeven else ""}
        </table>

        {trades_section}
        {price_section}
        {overview_section}
    </div>
    <div class="footer">
        Generated by Automa at {now_ist.strftime('%I:%M %p IST on %d %b %Y')}
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
    attachment_path: str | None = None,
) -> None:
    """Send an HTML email via SMTP (blocking — run in executor).

    If *attachment_path* is provided and the file exists, it is attached
    to the email as a text file.
    """
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr

    # HTML + plain-text alternative part
    alt_part = MIMEMultipart("alternative")
    plain = (
        f"{subject}\n\n"
        "This email is best viewed in an HTML-capable email client.\n"
        "Please enable HTML to see the full daily summary."
    )
    alt_part.attach(MIMEText(plain, "plain"))
    alt_part.attach(MIMEText(html_body, "html"))
    msg.attach(alt_part)

    # Attach daily log file if available
    if attachment_path:
        att_path = Path(attachment_path)
        if att_path.exists() and att_path.stat().st_size > 0:
            try:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(att_path.read_bytes())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={att_path.name}",
                )
                msg.attach(part)
            except Exception:
                logger.debug("Failed to attach log file %s", att_path, exc_info=True)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [to_addr], msg.as_string())
