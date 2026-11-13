"""Trade-detail emails for the standalone strategies (Day_Bout_Bdown, Scalping_1Min).

Uses the same email_settings in config.yaml and the same sender
(reporting._send_email) as trading_bot.py, so mail goes to the same inbox
with the same Gmail app password (${EMAIL_APP_PASSWORD} from .env).
Emails are sent on a background thread so the price loop is never blocked;
call flush() before the script exits so pending mail is delivered.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import os
import re
import threading
from pathlib import Path

import yaml
from dotenv import load_dotenv

from reporting import _send_email

ROOT = Path(__file__).resolve().parent
_threads: list[threading.Thread] = []
_config_cache: dict | None = None


def _substitute_env(obj):
    if isinstance(obj, dict):
        return {k: _substitute_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def _config() -> dict:
    global _config_cache
    if _config_cache is None:
        load_dotenv(ROOT / ".env")
        try:
            with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            _config_cache = {"email_settings": _substitute_env(raw.get("email_settings") or {})}
        except Exception as e:
            logging.getLogger(__name__).warning(f"Email disabled — could not read config.yaml: {e}")
            _config_cache = {}
    return _config_cache


def _pnl_html(v: float) -> str:
    color = "#1e8e3e" if v >= 0 else "#d93025"
    return f'<b style="color:{color}">{v:+,.2f}</b>'


def _table(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><td style='padding:4px 10px;color:#555'>{html.escape(k)}</td>"
                   f"<td style='padding:4px 10px'>{v}</td></tr>" for k, v in rows)
    return f"<table style='border-collapse:collapse;border:1px solid #ddd'>{body}</table>"


def _send_async(subject: str, body_html: str, logger: logging.Logger) -> None:
    cfg = _config()

    def worker():
        if not _send_email(cfg, subject, body_html):
            logger.warning(f"Email not sent: {subject}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    _threads.append(t)


def _page(title: str, content: str) -> str:
    return (f"<html><body style='font-family:Arial,sans-serif;color:#222'>"
            f"<h3 style='margin:0 0 10px'>{html.escape(title)}</h3>{content}"
            f"<p style='color:#888;font-size:12px'>Sent {dt.datetime.now():%Y-%m-%d %H:%M:%S}</p>"
            f"</body></html>")


def email_entry(strategy: str, paper: bool, logger: logging.Logger, details: dict) -> None:
    mode = "PAPER" if paper else "LIVE"
    subject = (f"[{strategy}] {mode} ENTRY {details.get('Side', '')} "
               f"{details.get('Symbol', '')} @ {details.get('Entry', '')}")
    rows = [(k, html.escape(str(v))) for k, v in details.items()]
    _send_async(subject, _page(f"{strategy} — {mode} entry", _table(rows)), logger)


def _summary_html(trades: list[dict], day_pnl: float, note: str = "") -> str:
    wins = sum(1 for t in trades if t.get("P&L", 0) > 0)
    out = _table([("Trades", str(len(trades))), ("Winners", str(wins)),
                  ("Losers", str(len(trades) - wins)), ("Day P&L", _pnl_html(day_pnl))]
                 + ([("Note", html.escape(note))] if note else []))
    if trades:
        cols = ["Time", "Side", "Symbol", "Qty", "Entry", "Exit", "Result", "P&L"]
        rows = "".join("<tr>" + "".join(
            f"<td>{_pnl_html(t[c]) if c == 'P&L' else html.escape(str(t.get(c, '')))}</td>" for c in cols)
            + "</tr>" for t in trades)
        out += ("<h4>Trades</h4><table border='1' cellpadding='5' style='border-collapse:collapse'>"
                "<tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in cols) + f"</tr>{rows}</table>")
    return out


def email_trade_closed(strategy: str, paper: bool, logger: logging.Logger,
                       details: dict, fills: list[dict], pnl: float,
                       day_trades: list[dict] | None = None) -> None:
    """details: summary fields; fills: [{Time, Action, Qty, Price, Reason, P&L}].
    day_trades: if given, a 'day summary so far' section is appended."""
    mode = "PAPER" if paper else "LIVE"
    subject = (f"[{strategy}] {mode} TRADE CLOSED {details.get('Symbol', '')} "
               f"| P&L {pnl:+,.2f}")
    rows = [(k, html.escape(str(v))) for k, v in details.items()] + [("Trade P&L", _pnl_html(pnl))]
    fill_rows = "".join(
        f"<tr><td>{html.escape(str(f.get('Time', '')))}</td><td>{html.escape(str(f.get('Action', '')))}</td>"
        f"<td>{f.get('Qty', '')}</td><td>{f.get('Price', '')}</td>"
        f"<td>{html.escape(str(f.get('Reason', '')))}</td>"
        f"<td>{_pnl_html(f['P&L']) if isinstance(f.get('P&L'), (int, float)) else ''}</td></tr>"
        for f in fills)
    fills_html = ("<h4>Fills</h4><table border='1' cellpadding='5' style='border-collapse:collapse'>"
                  "<tr><th>Time</th><th>Action</th><th>Qty</th><th>Price</th><th>Reason</th>"
                  f"<th>P&amp;L</th></tr>{fill_rows}</table>")
    if day_trades is not None:
        fills_html += ("<h4>Day summary so far</h4>"
                       + _summary_html(day_trades, sum(t.get("P&L", 0) for t in day_trades)))
    _send_async(subject, _page(f"{strategy} — {mode} trade closed", _table(rows) + fills_html), logger)


def email_day_summary(strategy: str, paper: bool, logger: logging.Logger,
                      trades: list[dict], day_pnl: float, note: str = "") -> None:
    """trades: [{Time, Side, Symbol, Qty, Entry, Exit, Result, P&L}]."""
    mode = "PAPER" if paper else "LIVE"
    subject = (f"[{strategy}] {mode} day summary {dt.date.today():%d %b %Y} | "
               f"trades {len(trades)} | P&L {day_pnl:+,.2f}")
    _send_async(subject, _page(f"{strategy} — {mode} day summary",
                               _summary_html(trades, day_pnl, note)), logger)


def flush(timeout: float = 30.0) -> None:
    """Wait for queued emails to finish sending (call before exiting)."""
    for t in _threads:
        t.join(timeout)
