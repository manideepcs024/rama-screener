"""
RAMA-MANIMINDSMONEY — NSE Intraday Momentum Screener + One-Click Trading

Run:
    streamlit run rama_screener.py

Credentials from .env:
    ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_ACCESS_TOKEN

Data sources:
    - Kite Connect  → real-time LTP, volume, % change  (live, every 60 s)
    - yfinance      → fundamentals + 20-day avg volume  (cached daily)

Trading features:
    - Buy/Sell buttons per row  |  Initial SL 3%  |  Target 3%
    - Trail SL / Trail Target by 0.25% on button click
    - Partial booking: 25 / 50 / 75 / 100 %
    - Paper trade toggle (default ON for safety)
    - Active positions panel above table with live P&L
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests as _requests

import pandas as pd
import streamlit as st
import yfinance as yf
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv
from kiteconnect import KiteConnect

import strategy_mailer as mailer

_log = logging.getLogger("rama_screener")
logging.basicConfig(level=logging.INFO)

# ── constants ─────────────────────────────────────────────────────────────────
ENV_PATH         = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH, override=True)   # load early so os.getenv picks up .env values
POSITIONS_FILE   = Path(__file__).resolve().parent / "output" / "rama_positions.json"
JOURNAL_FILE     = Path(__file__).resolve().parent / "output" / "trade_journal.json"
ALERTS_FILE      = Path(__file__).resolve().parent / "output" / "price_alerts.json"
NSE_N500_URL     = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_EQ_URL       = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_FNO_URL      = "https://archives.nseindia.com/content/fo/fo_mktlots.csv"
REFRESH_SECONDS  = 60 
MAX_FUND_WORKERS = 20
INIT_SL_PCT      = 3.0    # 3% SL from buy price
INIT_TGT_PCT     = 3.0    # 3% target from buy price
TRAIL_STEP_PCT   = 0.25   # each trail button click moves by 0.25%

_DEF_MARKET_CAP  = 5_000
_DEF_PE_MAX      = 60
_DEF_EPS_MIN     = 1.0
_DEF_INST_MIN    = 30.0
_DEF_MIN_PCT     = 2.0
_DEF_VOL_SURGE   = 1.5

# ── Telegram ──────────────────────────────────────────────────────────────────
def _tg_creds() -> tuple[str, str]:
    """Read token + chat_id directly from .env file each call, bypassing os.environ cache."""
    vals: dict[str, str] = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    vals[_k.strip()] = _v.strip()
    except Exception:
        pass
    tok = vals.get("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    cid = vals.get("TELEGRAM_CHAT_ID",   os.getenv("TELEGRAM_CHAT_ID",   "")).strip()
    return tok, cid

def _tg_enabled() -> bool:
    tok, cid = _tg_creds()
    return bool(tok and cid)

def send_telegram(text: str) -> bool:
    """Send a Markdown message to ALL configured chat IDs (comma-separated).
    TELEGRAM_CHAT_ID=your_id,friend_id,group_id
    Returns True if at least one send succeeded.
    """
    tok, cid_raw = _tg_creds()
    if not tok or not cid_raw:
        return False
    chat_ids = [c.strip() for c in cid_raw.split(",") if c.strip()]
    url      = f"https://api.telegram.org/bot{tok}/sendMessage"
    any_ok   = False
    for cid in chat_ids:
        try:
            resp = _requests.post(url, json={
                "chat_id":    cid,
                "text":       text,
                "parse_mode": "Markdown",
            }, timeout=5)
            if resp.ok:
                any_ok = True
            else:
                _log.warning(f"Telegram send to {cid} failed: {resp.text[:120]}")
        except Exception as e:
            _log.warning(f"Telegram send to {cid} error: {e}")
    return any_ok


# ── WhatsApp via Green API ─────────────────────────────────────────────────────
# Setup:
#   1. Register at console.green-api.com and create an instance
#   2. Scan QR code with your WhatsApp to link the instance
#   3. Add to .env:
#        GREENAPI_INSTANCE=710722800566
#        GREENAPI_TOKEN=f3b75e175c1441fa92ac23c41eb243ad6eceed0d557441b4a1
#        WHATSAPP_PHONE=+919887818001   (comma-separated for multiple recipients)

def _wa_creds() -> tuple[str, str, str]:
    """Returns (instance_id, api_token, phone_csv)."""
    vals: dict[str, str] = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    vals[_k.strip()] = _v.strip()
    except Exception:
        pass
    instance = vals.get("GREENAPI_INSTANCE", os.getenv("GREENAPI_INSTANCE", "")).strip()
    token    = vals.get("GREENAPI_TOKEN",    os.getenv("GREENAPI_TOKEN",    "")).strip()
    phone    = vals.get("WHATSAPP_PHONE",    os.getenv("WHATSAPP_PHONE",    "")).strip()
    return instance, token, phone


def _wa_enabled() -> bool:
    instance, token, phone = _wa_creds()
    return bool(instance and token and phone)


def _md_to_plain(text: str) -> str:
    """Strip Telegram Markdown (*bold*, _italic_, \. escapes) → plain text."""
    import re
    text = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", text)   # *bold*
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)",   r"\1", text)   # _italic_
    text = text.replace("\\.", ".").replace("\\_", "_").replace("\\!", "!")
    return text


def _phone_to_chat_id(phone: str) -> str:
    """Convert +919887818001 → 919887818001@c.us (Green API chatId format)."""
    digits = "".join(c for c in phone if c.isdigit())
    return f"{digits}@c.us"


def send_whatsapp(text: str) -> bool:
    """Send a plain-text message via Green API WhatsApp.
    GREENAPI_INSTANCE=710722800566
    GREENAPI_TOKEN=<token>
    WHATSAPP_PHONE=+91XXXXXXXXXX   (comma-separated for multiple recipients)
    Returns True if at least one send succeeded.
    """
    instance, token, phone_raw = _wa_creds()
    if not instance or not token or not phone_raw:
        return False
    plain  = _md_to_plain(text)
    phones = [p.strip() for p in phone_raw.split(",") if p.strip()]
    any_ok = False
    for phone in phones:
        try:
            url  = f"https://api.green-api.com/waInstance{instance}/sendMessage/{token}"
            body = {"chatId": _phone_to_chat_id(phone), "message": plain}
            resp = _requests.post(url, json=body, timeout=15)
            if resp.ok and resp.json().get("idMessage"):
                any_ok = True
            else:
                _log.warning(f"WhatsApp (Green API) send to {phone} failed: {resp.text[:120]}")
        except Exception as e:
            _log.warning(f"WhatsApp (Green API) send to {phone} error: {e}")
    return any_ok


def send_alert(text: str) -> bool:
    """Send auto-alert to Telegram only. WhatsApp/Twitter are manual-button only."""
    return send_telegram(text) if _tg_enabled() else False


# ── Twitter / X ───────────────────────────────────────────────────────────────
# Setup:
#   1. developer.twitter.com → create project + app (Free tier = 1500 tweets/month)
#   2. App permissions → Read and Write
#   3. Keys and Tokens → generate all 4 keys
#   4. Add to .env:
#        TWITTER_API_KEY=xxx
#        TWITTER_API_SECRET=xxx
#        TWITTER_ACCESS_TOKEN=xxx
#        TWITTER_ACCESS_TOKEN_SECRET=xxx

_STRATEGY_HASHTAGS: dict[str, list[str]] = {
    "Swing Trade (3-5 day)":      ["#SwingTrade", "#SwingTrading", "#StockSetup"],
    "Buying Pressure":             ["#BuyingPressure", "#Accumulation"],
    "Momentum (already moving)":   ["#Momentum", "#MomentumTrading", "#Breakout"],
    "Pre-Breakout (coiling)":      ["#Breakout", "#Coiling", "#StockSetup"],
    "VCP Setup":                   ["#VCP", "#VolatilityContraction", "#StockSetup"],
    "Inside Bar":                  ["#InsideBar", "#PriceAction", "#StockSetup"],
    "Range Breakout (5-day)":      ["#RangeBreakout", "#Breakout"],
    "Custom (manual filters)":     ["#StockAlert"],
}
_COMMON_HASHTAGS = ["#NSE", "#IndianStockMarket", "#StockMarket", "#Nifty"]


def _tw_creds() -> tuple[str, str, str, str]:
    vals: dict[str, str] = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    vals[_k.strip()] = _v.strip()
    except Exception:
        pass
    return (
        vals.get("TWITTER_API_KEY",              os.getenv("TWITTER_API_KEY",              "")).strip(),
        vals.get("TWITTER_API_SECRET",           os.getenv("TWITTER_API_SECRET",           "")).strip(),
        vals.get("TWITTER_ACCESS_TOKEN",         os.getenv("TWITTER_ACCESS_TOKEN",         "")).strip(),
        vals.get("TWITTER_ACCESS_TOKEN_SECRET",  os.getenv("TWITTER_ACCESS_TOKEN_SECRET",  "")).strip(),
    )


def _tw_enabled() -> bool:
    return all(_tw_creds())


def _build_tweet(sym: str, name: str, ltp: float, pct: float, vol_surge: float,
                 score: float, sl: float, target: float,
                 strategy: str, paper: bool) -> str:
    """Compose a tweet ≤ 280 chars with signal details and hashtags."""
    tag       = "📄 PAPER" if paper else "🔴 LIVE"
    prob      = "🟢" if score >= 70 else ("🟡" if score >= 40 else "🔴")
    strat_tags = _STRATEGY_HASHTAGS.get(strategy, ["#StockAlert"])
    sym_tag    = f"#{sym}"
    # build hashtag line — keep total tweet under 280
    all_tags   = [sym_tag] + strat_tags + _COMMON_HASHTAGS
    tag_line   = " ".join(all_tags)

    body = (
        f"🏦 MANIMINDSMONEY | RAMA  {tag}\n"
        f"\n"
        f"{prob} ${sym} — {name[:22]}\n"
        f"CMP ₹{ltp:,.2f}  {pct:+.1f}%  Vol {vol_surge:.1f}×  Score {score:.0f}\n"
        f"\n"
        f"🎯 Target ₹{target:,.2f}\n"
        f"🛑 SL      ₹{sl:,.2f}\n"
        f"\n"
    )
    # trim tag line if total exceeds 280
    tweet = body + tag_line
    if len(tweet) > 280:
        # drop common hashtags one by one until it fits
        tags = [sym_tag] + strat_tags + _COMMON_HASHTAGS
        while len(body + " ".join(tags)) > 280 and len(tags) > 1:
            tags.pop()
        tweet = body + " ".join(tags)
    return tweet[:280]


def post_tweet(sym: str, name: str, ltp: float, pct: float, vol_surge: float,
               score: float, sl: float, target: float,
               strategy: str, paper: bool) -> bool:
    """Post a signal tweet via Twitter API v2 (OAuth 1.0a). Returns True on success."""
    api_key, api_secret, acc_token, acc_secret = _tw_creds()
    if not all([api_key, api_secret, acc_token, acc_secret]):
        return False
    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=acc_token,
            access_token_secret=acc_secret,
        )
        text = _build_tweet(sym, name, ltp, pct, vol_surge, score, sl, target, strategy, paper)
        client.create_tweet(text=text)
        return True
    except Exception as e:
        _log.warning(f"Tweet failed for {sym}: {e}")
        return False


def _tg_reply(chat_id: str, text: str) -> bool:
    """Send a Markdown message to one specific chat_id."""
    tok, _ = _tg_creds()
    if not tok:
        return False
    try:
        r = _requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5,
        )
        return r.ok
    except Exception as e:
        _log.warning(f"Telegram reply failed: {e}")
        return False


def _tg_get_updates(offset: int = 0) -> list[dict]:
    """Poll Telegram for new messages since `offset`."""
    tok, _ = _tg_creds()
    if not tok:
        return []
    try:
        r = _requests.get(
            f"https://api.telegram.org/bot{tok}/getUpdates",
            params={"offset": offset, "timeout": 0, "limit": 20},
            timeout=6,
        )
        if r.ok:
            return r.json().get("result", [])
    except Exception as e:
        _log.warning(f"Telegram getUpdates failed: {e}")
    return []


def _build_pnl_reply(kite) -> str:
    """Fetch live positions from Zerodha and format a P&L summary for Telegram."""
    try:
        pos_data   = kite.positions()
        day_pos    = pos_data.get("day", [])
        net_pos    = pos_data.get("net", [])
        realised   = sum(float(p.get("realised", 0) or 0) for p in day_pos)
        unrealised = sum(float(p.get("unrealised", 0) or 0) for p in net_pos)
        total      = realised + unrealised

        r_sign = "+" if realised   >= 0 else ""
        u_sign = "+" if unrealised >= 0 else ""
        t_sign = "+" if total      >= 0 else ""
        t_icon = "🟢" if total >= 0 else "🔴"

        lines = [
            "🏦 *MANIMINDSMONEY | RAMA*",
            "─" * 28,
            f"💰 *P&L SNAPSHOT — {datetime.now():%d %b %Y %H:%M}*",
            "─" * 28,
            f"📗 Day Booked P&L:  *{r_sign}₹{realised:,.0f}*",
            f"📊 MTM (Open):       *{u_sign}₹{unrealised:,.0f}*",
            f"{t_icon} *Total Day P&L:   {t_sign}₹{total:,.0f}*",
            "─" * 28,
        ]

        # individual open positions
        open_pos = [p for p in net_pos if int(p.get("quantity", 0)) != 0]
        if open_pos:
            lines.append("📋 *Open Positions:*")
            for p in open_pos:
                _sym  = p.get("tradingsymbol", "?")
                _qty  = int(p.get("quantity", 0))
                _avg  = float(p.get("average_price", 0) or 0)
                _ltp  = float(p.get("last_price", 0) or 0)
                _pnl  = float(p.get("pnl", 0) or 0)
                _unrl = float(p.get("unrealised", 0) or 0)
                _icon = "🟢" if _pnl >= 0 else "🔴"
                _sign = "+" if _pnl >= 0 else ""
                lines.append(
                    f"{_icon} *{_sym}*  Qty:{_qty}  Avg:₹{_avg:.1f}  LTP:₹{_ltp:.1f}  "
                    f"P&L:{_sign}₹{_pnl:,.0f}"
                )
        else:
            lines.append("_No open positions_")

        # closed trades today
        closed = [p for p in day_pos if int(p.get("quantity", 0)) == 0
                  and float(p.get("realised", 0) or 0) != 0]
        if closed:
            lines.append("─" * 28)
            lines.append("✅ *Closed Today:*")
            for p in closed:
                _sym  = p.get("tradingsymbol", "?")
                _real = float(p.get("realised", 0) or 0)
                _icon = "🟢" if _real >= 0 else "🔴"
                _sign = "+" if _real >= 0 else ""
                lines.append(f"{_icon} *{_sym}*  Booked: {_sign}₹{_real:,.0f}")

        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ Could not fetch P&L: {e}"


def check_telegram_commands(kite) -> None:
    """
    Poll Telegram for new messages. Supported commands (case-insensitive):
      pnl / /pnl  →  reply with live Zerodha P&L snapshot
    Runs at most once every 15 seconds to avoid rate-limiting.
    """
    if not _tg_enabled():
        return
    _now = time.time()
    if _now - st.session_state.get("_tg_cmd_ts", 0) < 15:
        return
    st.session_state["_tg_cmd_ts"] = _now

    offset = st.session_state.get("_tg_update_offset", 0)
    updates = _tg_get_updates(offset)
    for upd in updates:
        upd_id = upd.get("update_id", 0)
        st.session_state["_tg_update_offset"] = upd_id + 1

        msg  = upd.get("message") or upd.get("channel_post") or {}
        text = (msg.get("text") or "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not chat_id:
            continue

        if text in ("pnl", "/pnl", "/pnl@ramabot"):
            reply = _build_pnl_reply(kite)
            _tg_reply(chat_id, reply)


def tg_signal(sym: str, name: str, cmp: float, sl: float, target: float,
              vol_surge: float, score: float, strategy: str,
              paper: bool = True) -> None:
    """Send a BUY signal message to Telegram."""
    sl_pct  = (cmp - sl)     / cmp * 100
    tgt_pct = (target - cmp) / cmp * 100
    prob    = "🟢 HIGH" if score >= 70 else ("🟡 MEDIUM" if score >= 40 else "🔴 LOW")
    tag     = "📄 PAPER" if paper else "🔴 LIVE"
    ind15   = get_15min_indicators(sym)
    rsi_15  = ind15["rsi_15"]
    vwap_15 = ind15["vwap_15"]
    av15    = ind15["above_vwap_15"]
    vwap_line = (
        f"📐 VWAP (15m): ₹{vwap_15:,.2f}  {'✅ Above' if av15 else '❌ Below'}\n"
        if av15 is not None else ""
    )
    msg = (
        f"🏦 *MANIMINDSMONEY | RAMA*\n"
        f"{'─'*28}\n"
        f"📊 *SIGNAL — {sym}*\n"
        f"_{name}_\n"
        f"{'─'*28}\n"
        f"📍 CMP:        ₹{cmp:,.2f}\n"
        f"🎯 Target:     ₹{target:,.2f}  _(+{tgt_pct:.1f}%)_\n"
        f"🛑 Stop Loss:  ₹{sl:,.2f}  _(-{sl_pct:.1f}%)_\n"
        f"📦 Vol Surge:  {vol_surge:.1f}×\n"
        f"📉 RSI (15m):  {rsi_15:.1f}\n"
        f"{vwap_line}"
        f"📈 Score:      {score:.0f}/100\n"
        f"⚡ Probability: {prob}\n"
        f"🎲 Strategy:   {strategy}\n"
        f"{'─'*28}\n"
        f"{tag}  ·  {datetime.now():%d %b %Y %H:%M}"
    )
    send_alert(msg)


def tg_top5(final: pd.DataFrame, strategy: str, paper: bool) -> None:
    """Auto-send top-10 stocks ranked by volume-weighted signal score.

    Score formula: vol_surge 60% + pct_change 40% (both normalised).
    Only stocks with pct_change >= 2% qualify.
    Fires at most once every 5 minutes AND only when the top-10 symbols change.
    """
    if not _tg_enabled() or final.empty:
        return

    # ── build signal score ────────────────────────────────────────────────────
    df = final[final["pct_change"] >= 2.0].copy()
    if df.empty:
        return
    _max_vol = max(df["vol_surge"].max(), 1)
    _max_pct = max(df["pct_change"].max(), 1)
    df["_sig"] = (df["vol_surge"] / _max_vol * 60 +
                  df["pct_change"] / _max_pct * 40)
    top5 = df.nlargest(10, "_sig").reset_index(drop=True)

    # ── rate-limit: 5 min interval + de-dup by symbol list ───────────────────
    _now     = time.time()
    _last_ts = st.session_state.get("_tg_top5_ts", 0)
    _last_syms = st.session_state.get("_tg_top5_syms", [])
    _cur_syms  = top5["symbol"].tolist()
    if _now - _last_ts < 300 and _cur_syms == _last_syms:
        return                              # nothing changed, skip

    st.session_state["_tg_top5_ts"]   = _now
    st.session_state["_tg_top5_syms"] = _cur_syms

    # ── build message ─────────────────────────────────────────────────────────
    tag   = "📄 PAPER" if paper else "🔴 LIVE"
    lines = [
        f"🏦 *MANIMINDSMONEY | RAMA*",
        "─" * 30,
        f"🏆 *TOP 10 SIGNALS — {strategy[:20]}*",
        f"_{datetime.now():%d %b %Y %H:%M}_  |  {tag}",
        "─" * 30,
    ]
    for i, row in top5.iterrows():
        sym    = row["symbol"]
        name   = str(row.get("name", sym))[:18]
        cmp    = row["ltp"]
        sl     = round(cmp * (1 - INIT_SL_PCT  / 100), 2)
        tgt    = round(cmp * (1 + INIT_TGT_PCT / 100), 2)
        vol    = row["vol_surge"]
        pct    = row["pct_change"]
        score  = row.get("score", 0)
        prob   = "🟢" if score >= 70 else ("🟡" if score >= 40 else "🔴")
        ind15  = get_15min_indicators(sym)
        rsi15  = ind15["rsi_15"]
        av15   = ind15["above_vwap_15"]
        vwap_tag = ("✅" if av15 else "❌") if av15 is not None else "—"
        lines.append(
            f"{i+1}\\. {prob} *{sym}* _{name}_\n"
            f"   CMP ₹{cmp:,.2f}  |  Chg {pct:+.1f}%  |  Vol {vol:.1f}×\n"
            f"   RSI(15m) {rsi15:.0f}  |  VWAP {vwap_tag}  |  Score {score:.0f}\n"
            f"   🎯 ₹{tgt:,.2f}  🛑 ₹{sl:,.2f}"
        )
        lines.append("─" * 30)   # separator after each signal
    send_alert("\n".join(lines))


def tg_exit(sym: str, name: str, entry: float, exit_price: float,
            qty: int, reason: str, paper: bool = True) -> None:
    """Send SL-hit / target-hit / manual-exit alert to Telegram."""
    pnl      = (exit_price - entry) * qty
    pnl_pct  = (exit_price - entry) / entry * 100
    icon     = "✅" if pnl >= 0 else "🔴"
    tag      = "📄 PAPER" if paper else "🔴 LIVE"
    reason_u = reason.upper()
    msg = (
        f"🏦 *MANIMINDSMONEY | RAMA*\n"
        f"{'─'*28}\n"
        f"{icon} *EXIT — {sym}*\n"
        f"_{name}_  |  _{reason_u}_\n"
        f"{'─'*28}\n"
        f"💰 Exit:   ₹{exit_price:,.2f}\n"
        f"📌 Entry:  ₹{entry:,.2f}\n"
        f"📦 Qty:    {qty}\n"
        f"{'🟢' if pnl>=0 else '🔴'} P&L:   ₹{pnl:+,.2f}  _({pnl_pct:+.1f}%)_\n"
        f"{'─'*28}\n"
        f"{tag}  ·  {datetime.now():%d %b %Y %H:%M}"
    )
    send_alert(msg)


# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="RAMA-MANIMINDSMONEY", page_icon="📈", layout="wide")

st.markdown(
    """<style>
    #flonnect-my-app { display: none !important; }

    /* Prevent horizontal scroll from wide table */
    .main .block-container { overflow-x: hidden !important; max-width: 100% !important; }
    /* Mobile column labels: hidden on desktop */
    .mh { display: none; }

    /* ── Global mobile layout ─────────────────────────────────── */
    @media (max-width: 768px) {
        /* Full width layout */
        .block-container { padding: 0.5rem 0.5rem !important; }
        /* Metric tiles smaller */
        div[data-testid="stMetric"] { padding: 4px !important; }
        div[data-testid="stMetric"] label { font-size: 10px !important; }
        div[data-testid="stMetric"] div[data-testid="stMetricValue"] { font-size: 14px !important; }
        /* Account banner wraps */
        div[style*="display:flex"] { flex-wrap: wrap !important; }
        /* Tabs font */
        button[data-baseweb="tab"] { font-size: 11px !important; padding: 4px 8px !important; }
    }
    </style>""",
    unsafe_allow_html=True,
)

st.markdown(
    """
    <h1 style='text-align:center;color:#1a73e8;letter-spacing:3px;
               font-family:monospace;padding-bottom:2px'>
    📈 &nbsp; RAMA-MANIMINDSMONEY
    </h1>
    <p style='text-align:center;color:#888;margin-top:0;font-size:14px'>
    NSE Intraday Momentum Screener &nbsp;·&nbsp; Real-time via Kite Connect
    </p>
    <hr style='border:1px solid #eee;margin-bottom:4px'>
    """,
    unsafe_allow_html=True,
)

# ── session state init ────────────────────────────────────────────────────────
if "positions" not in st.session_state:
    st.session_state.positions = {}   # {symbol: position_dict}

# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")

    PAPER_TRADE = st.toggle("📄 Paper Trade Mode", value=True,
                            help="OFF = live Kite orders. ON = simulated only.")
    if PAPER_TRADE:
        st.info("Paper trade ON — no real orders placed.")
    else:
        st.warning("⚠️ LIVE mode — real orders will be placed!")

    st.subheader("Universe")
    UNIVERSE_CHOICE = st.selectbox(
        "Stock universe",
        ["Nifty F&O", "Futures – Current Month", "Futures – Next Month",
         "Nifty 500", "All NSE Cash"],
        help="F&O = ~180 liquid stocks. Futures = stocks with active futures contracts (lot size as default qty). Nifty 500 = broad market.",
    )
    if UNIVERSE_CHOICE == "Nifty F&O":
        st.caption("~180 F&O stocks — most liquid, options-tradeable.")
    elif UNIVERSE_CHOICE == "Futures – Current Month":
        st.caption("Stock futures expiring this month — qty defaults to lot size.")
    elif UNIVERSE_CHOICE == "Futures – Next Month":
        st.caption("Stock futures expiring next month — qty defaults to lot size.")
    elif UNIVERSE_CHOICE == "All NSE Cash":
        st.warning("⚠️ ~1800 stocks — first fundamental load may take 2-3 min (cached after).")

    # ── strategy selector ─────────────────────────────────────────────────────
    st.subheader("Strategy")
    STRATEGY = st.selectbox(
        "Select strategy",
        [
            "Swing Trade (3-5 day)",
            "Buying Pressure",
            "Momentum (already moving)",
            "Pre-Breakout (coiling)",
            "VCP Setup",
            "Inside Bar",
            "Range Breakout (5-day)",
            "Custom (manual filters)",
        ],
        index=0,
        key="k_strategy",
    )

    # ── strategy descriptions ─────────────────────────────────────────────────
    _STRATEGY_DESC = {
        "Swing Trade (3-5 day)":
            "High-probability 3-5 day setup: EMA aligned + RSI 50-65 + volume + near 52W high + ADX trend — sorted by Swing Score.",
        "Buying Pressure":
            "65%+ of last 10 days were up-candles AND today is up — consistent accumulation based on actual traded volume, not order book.",
        "Momentum (already moving)":
            "% change ≥2% AND volume surge ≥1.5× — stocks already in motion.",
        "Pre-Breakout (coiling)":
            "BB Squeeze OR NR7 OR VCP OR Inside Bar — compression before the move.",
        "VCP Setup":
            "Volatility Contraction Pattern — 3 successive daily range contractions.",
        "Inside Bar":
            "Today's range inside yesterday's — spring loaded for a directional break.",
        "Range Breakout (5-day)":
            "Close broke above the 5-day high with volume — short-term momentum.",
        "Custom (manual filters)":
            "Configure every filter individually below.",
    }
    st.caption(_STRATEGY_DESC[STRATEGY])

    # ── strategy sets the PATTERN flags (OR logic in filter) ─────────────────
    _S = STRATEGY
    USE_BB_SQUEEZE     = _S in ("Pre-Breakout (coiling)",)
    USE_NR7            = _S in ("Pre-Breakout (coiling)",)
    USE_TIGHT_INTRADAY = _S in ("Pre-Breakout (coiling)",)
    USE_SETUP_RSI      = _S in ("Pre-Breakout (coiling)",)
    USE_INSIDE_BAR     = _S in ("Pre-Breakout (coiling)", "Inside Bar")
    USE_VCP            = _S in ("Pre-Breakout (coiling)", "VCP Setup")
    USE_RANGE_BO       = _S in ("Range Breakout (5-day)",)
    USE_NEAR_DAY_HIGH  = False   # always manual
    IS_SWING           = _S == "Swing Trade (3-5 day)"
    IS_BUYING_PRESSURE = _S == "Buying Pressure"

    if _S == "Custom (manual filters)":
        st.subheader("Pattern Signals (OR)")
        USE_BB_SQUEEZE     = st.toggle("BB Squeeze",                     value=False, key="k_bbs")
        USE_NR7            = st.toggle("NR7 — Narrowest Range 7d",       value=False, key="k_nr7")
        USE_NEAR_DAY_HIGH  = st.toggle("Near day's high (≤0.5%)",        value=False, key="k_ndh")
        USE_TIGHT_INTRADAY = st.toggle("Tight intraday range (<1.5%)",   value=False, key="k_tir")
        USE_SETUP_RSI      = st.toggle("RSI building (45–60)",           value=False, key="k_sri")
        USE_INSIDE_BAR     = st.toggle("Inside Bar",                     value=False, key="k_ib")
        USE_RANGE_BO       = st.toggle("Range Breakout 5D",              value=False, key="k_rbo")
        USE_VCP            = st.toggle("VCP",                            value=False, key="k_vcp")

    # ── technical filter — always visible for all strategies ─────────────────
    # Widget keys are scoped to the strategy so defaults reset when strategy
    # changes (Streamlit ignores value= for existing keys; a new key forces reset).
    _sk = _S.replace(" ", "_").replace("(", "").replace(")", "").replace("-", "")
    _pct_default  = _S == "Momentum (already moving)"
    _vol_default  = _S in ("Momentum (already moving)", "Range Breakout (5-day)", "Buying Pressure")
    _ema21_def    = IS_SWING
    _ema50_def    = IS_SWING
    _ema200_def   = IS_SWING
    _rsi_def      = IS_SWING
    _rsi_rng_def  = (50, 72) if IS_SWING else (30, 70)
    st.subheader("Technical Filter")
    USE_PCT_FILTER = st.toggle("Enable % change filter",      value=_pct_default, key=f"k_pct_on_{_sk}")
    MIN_PCT_CHANGE = st.slider("Min price change %", 0.5, 10.0, _DEF_MIN_PCT,  0.5,
                               disabled=not USE_PCT_FILTER, key=f"k_pct_val_{_sk}")
    USE_VOL_FILTER = st.toggle("Enable volume surge filter",  value=_vol_default, key=f"k_vol_on_{_sk}")
    VOL_SURGE_MIN  = st.slider("Min volume surge ×",  1.0,  5.0, _DEF_VOL_SURGE, 0.25,
                               disabled=not USE_VOL_FILTER, key=f"k_vol_val_{_sk}")
    USE_RSI_FILTER = st.toggle("Enable RSI range filter",     value=_rsi_def,     key=f"k_rsi_on_{_sk}")
    RSI_RANGE      = st.slider("RSI range", 0, 100, _rsi_rng_def, 1,
                               disabled=not USE_RSI_FILTER, key=f"k_rsi_rng_{_sk}")
    RSI_MIN, RSI_MAX = RSI_RANGE
    USE_VWAP_FILTER = st.toggle("Enable VWAP filter (15-min)",  value=False,        key=f"k_vwap_on_{_sk}",
                               help="Uses cumulative 15-min chart VWAP for the trading day")
    VWAP_SIDE       = st.selectbox("Price vs 15-min VWAP", ["Above VWAP", "Below VWAP"],
                                   disabled=not USE_VWAP_FILTER, key=f"k_vwap_side_{_sk}")
    USE_BS_FILTER   = st.toggle("Enable Buy Pressure filter", value=IS_BUYING_PRESSURE, key=f"k_bs_on_{_sk}")
    BS_RATIO_MIN    = st.slider("Min Buy Pressure %", 50, 100, 65, 5,
                                disabled=not USE_BS_FILTER, key=f"k_bs_val_{_sk}",
                                help="% of last 10 days + today that were up-candles. 65%+ = buyers dominating") / 100.0

    st.subheader("EMA Filter")
    st.caption("Price must be above selected EMAs (AND logic — all ticked must pass)")
    USE_EMA21  = st.toggle("Above 21 EMA",  value=_ema21_def,  key=f"k_ema21_{_sk}")
    USE_EMA50  = st.toggle("Above 50 EMA",  value=_ema50_def,  key=f"k_ema50_{_sk}")
    USE_EMA200 = st.toggle("Above 200 EMA", value=_ema200_def, key=f"k_ema200_{_sk}")
    if USE_EMA21 or USE_EMA50 or USE_EMA200:
        st.caption("⚠️ EMA data needs 1y yfinance history — cached daily after first load.")

    st.subheader("News & Results Filter")
    USE_NEWS_FILTER      = st.toggle("Positive news sentiment only",
                                     value=False, key=f"k_news_{_sk}",
                                     help="Keeps stocks with positive avg headline sentiment (last 2 days)")
    USE_RESULTS_FILTER   = st.toggle("Has recent results (last 7 days)",
                                     value=False, key=f"k_results_{_sk}",
                                     help="Keeps stocks that announced quarterly earnings recently")
    if USE_NEWS_FILTER or USE_RESULTS_FILTER:
        st.caption("⏳ News data cached 2 h — first load fetches headlines per stock.")

    st.subheader("Fundamental Filter")
    USE_FUNDAMENTAL   = st.toggle("Enable fundamental filter", value=False)
    MARKET_CAP_MIN_CR = st.number_input("Min market cap (Cr)", value=_DEF_MARKET_CAP, step=500,
                                        disabled=not USE_FUNDAMENTAL)
    PE_MAX            = st.slider("Max PE ratio",    10, 120, _DEF_PE_MAX,  5,
                                  disabled=not USE_FUNDAMENTAL)
    EPS_MIN           = st.number_input("Min EPS",   value=_DEF_EPS_MIN,  step=0.5,
                                        disabled=not USE_FUNDAMENTAL)
    INST_MIN_PCT      = st.number_input("Min institutional %", value=_DEF_INST_MIN, step=5.0,
                                        disabled=not USE_FUNDAMENTAL)
    st.markdown("---")
    st.subheader("Telegram")
    if _tg_enabled():
        _, _cid_raw = _tg_creds()
        _n_rcpt = len([c for c in _cid_raw.split(",") if c.strip()])
        st.success(f"✅ Telegram connected ({_n_rcpt} recipient{'s' if _n_rcpt != 1 else ''})")
        st.caption("Add more IDs: TELEGRAM_CHAT_ID=id1,id2,id3 in .env")
    else:
        st.warning("⚠️ Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env")
        st.caption("Get token from @BotFather · Chat ID from @userinfobot")
    # WhatsApp status
    if _wa_enabled():
        _wa_inst, _, _wa_phone = _wa_creds()
        _n_wa = len([p for p in _wa_phone.split(",") if p.strip()])
        st.success(f"✅ WhatsApp (Green API) connected — instance {_wa_inst} · {_n_wa} number{'s' if _n_wa!=1 else ''}")
        st.caption("GREENAPI_INSTANCE · GREENAPI_TOKEN · WHATSAPP_PHONE in .env")
    else:
        st.info("💬 WhatsApp alerts: add Green API keys to .env  (console.green-api.com)")
        st.caption("GREENAPI_INSTANCE=710722800566  GREENAPI_TOKEN=<token>  WHATSAPP_PHONE=+91XXXXXXXXXX")

    # Twitter / X status
    if _tw_enabled():
        st.success("✅ Twitter / X connected")
        st.caption("TWITTER_API_KEY · API_SECRET · ACCESS_TOKEN · ACCESS_TOKEN_SECRET in .env")
    else:
        st.info("🐦 Twitter alerts: add 4 keys to .env  (developer.twitter.com → Free tier)")
        st.caption("TWITTER_API_KEY · TWITTER_API_SECRET · TWITTER_ACCESS_TOKEN · TWITTER_ACCESS_TOKEN_SECRET")

    if st.button("🔔 Test Telegram", key="tg_test"):
        ok = send_telegram("🏦 *MANIMINDSMONEY | RAMA*\n✅ Screener connected — Telegram alerts active!")
        st.toast("Telegram OK ✅" if ok else "Telegram failed ❌ — check .env keys")
    if st.button("📱 Test WhatsApp", key="wa_test", disabled=not _wa_enabled()):
        ok = send_whatsapp("MANIMINDSMONEY | RAMA\nWhatsApp alerts active!")
        st.toast("WhatsApp OK ✅" if ok else "WhatsApp failed ❌ — check .env keys")
    if st.button("🐦 Test Twitter", key="tw_test", disabled=not _tw_enabled()):
        ok = post_tweet("NIFTY", "Index Test", 24000.0, 0.5, 1.2, 75.0,
                        23280.0, 24720.0,
                        st.session_state.get("k_strategy", "Momentum"), True)
        st.toast("Twitter OK ✅" if ok else "Twitter failed ❌ — check .env keys")
    if "tg_custom_msg" not in st.session_state:
        st.session_state["tg_custom_msg"] = ""

    def _send_custom():
        msg = st.session_state.get("tg_custom_msg", "").strip()
        if msg:
            ok = send_telegram(msg)
            st.session_state["tg_custom_msg"] = ""
            st.session_state["_tg_send_ok"] = ok

    custom_msg = st.text_area("Custom message", placeholder="Type a message to send to Telegram…",
                              key="tg_custom_msg", height=80)
    st.button("📤 Send Custom", key="tg_custom_send",
              disabled=not (_tg_enabled() and bool(custom_msg.strip())),
              on_click=_send_custom)
    if "_tg_send_ok" in st.session_state:
        ok = st.session_state.pop("_tg_send_ok")
        st.toast("Sent ✅" if ok else "Failed ❌ — check .env keys")

    st.markdown("---")
    st.caption(f"Auto-refresh every {REFRESH_SECONDS} s")
    st.caption("🟢 Score ≥ 70  🟡 Score ≥ 40  ⚪ Score < 40")


# ── Kite auth ─────────────────────────────────────────────────────────────────
_TOKEN_FILE = Path(__file__).resolve().parent / "output" / "kite_token.json"


def _save_token(token: str) -> None:
    """Save token to both .env (local) and output/kite_token.json (cloud shared)."""
    # local .env
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        out, found = [], False
        for line in lines:
            if line.startswith("ZERODHA_ACCESS_TOKEN="):
                out.append(f"ZERODHA_ACCESS_TOKEN={token}"); found = True
            else:
                out.append(line)
        if not found:
            out.append(f"ZERODHA_ACCESS_TOKEN={token}")
        try:
            ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
        except Exception:
            pass
    # shared file — persists across browser sessions on the same server instance
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _j
        _j.dump({"token": token, "saved_at": datetime.now().isoformat()},
                open(_TOKEN_FILE, "w"))
    except Exception:
        pass
    os.environ["ZERODHA_ACCESS_TOKEN"] = token


def _load_saved_token() -> str:
    """Read token from shared file first, fallback to env var."""
    try:
        if _TOKEN_FILE.exists():
            import json as _j
            data = _j.load(open(_TOKEN_FILE))
            return data.get("token", "").strip()
    except Exception:
        pass
    return os.getenv("ZERODHA_ACCESS_TOKEN", "").strip()


def _kite_connect() -> KiteConnect:
    load_dotenv(ENV_PATH, override=True)
    api_key    = os.getenv("ZERODHA_API_KEY", "").strip()
    api_secret = os.getenv("ZERODHA_API_SECRET", "").strip()
    token      = _load_saved_token()
    if not api_key:
        st.error("ZERODHA_API_KEY missing in .env / secrets"); st.stop()

    kite = KiteConnect(api_key=api_key)
    if token:
        kite.set_access_token(token)
        try:
            kite.profile()
            return kite
        except Exception:
            pass

    # ── Token expired — show update UI ────────────────────────────────────────
    st.warning("⚠️ Kite access token expired or not set.")
    st.markdown("### Update Zerodha Access Token")
    st.info(
        "**How to get today's token:**\n"
        "1. Open Kite app / kite.zerodha.com on your phone\n"
        "2. You are already logged in — the session is active\n"
        "3. Copy the access token from Settings → API or ask the account holder\n\n"
        "**OR** log in via URL below to auto-generate:"
    )

    tab1, tab2 = st.tabs(["📋 Paste Access Token directly", "🔗 Login via Zerodha URL"])

    with tab1:
        st.caption("Paste the access token (the account holder can share this daily)")
        _direct_token = st.text_input("Access Token", type="password",
                                      placeholder="Paste access token here…",
                                      key="direct_token_input")
        if st.button("✅ Set Token", key="set_direct_token", type="primary",
                     disabled=not _direct_token):
            kite.set_access_token(_direct_token.strip())
            try:
                profile = kite.profile()
                _save_token(_direct_token.strip())
                st.success(f"✅ Token set! Logged in as **{profile.get('user_name')}**. Reloading…")
                time.sleep(1); st.rerun()
            except Exception as e:
                st.error(f"Invalid token: {e}")

    with tab2:
        st.markdown(f"[Click here to log in to Zerodha]({kite.login_url()})")
        raw = st.text_input("Paste redirect URL or request_token:", key="kite_redirect_url")
        if raw:
            req_token = (parse_qs(urlparse(raw).query).get("request_token", [""])[0]
                         if "request_token=" in raw else raw.strip())
            if not req_token:
                st.error("Could not extract request_token.")
            elif not api_secret:
                st.error("ZERODHA_API_SECRET missing in secrets.")
            else:
                try:
                    data = kite.generate_session(req_token, api_secret=api_secret)
                    kite.set_access_token(data["access_token"])
                    _save_token(data["access_token"])
                    st.success(f"✅ Logged in as **{data.get('user_name')}**. Reloading…")
                    time.sleep(1); st.rerun()
                except Exception as e:
                    st.error(f"Login failed: {e}")
    st.stop()


def get_kite() -> KiteConnect:
    now = time.time()
    if "kite" not in st.session_state or (now - st.session_state.get("kite_checked_at", 0)) > 1800:
        try:
            if "kite" in st.session_state:
                st.session_state["kite"].profile()
                st.session_state["kite_checked_at"] = now
            else:
                raise Exception("no kite")
        except Exception:
            st.session_state["kite"] = _kite_connect()
            st.session_state["kite_checked_at"] = now
    return st.session_state["kite"]


# ── account info ──────────────────────────────────────────────────────────────
def fetch_account_info(kite: KiteConnect) -> dict:
    info = {
        "name": "Trader",
        "cash": 0.0, "used": 0.0, "net": 0.0, "collateral": 0.0,
        "day_realised": 0.0, "day_unrealised": 0.0,
        "day_positions": [],
    }
    try:
        info["name"] = kite.profile().get("user_name", "Trader")
    except Exception:
        pass
    try:
        m = kite.margins("equity")
        info["cash"]       = float(m.get("available", {}).get("live_balance", 0) or 0)
        info["collateral"] = float(m.get("available", {}).get("collateral", 0) or 0)
        info["used"]       = float(m.get("utilised",  {}).get("debits", 0) or 0)
        info["net"]        = float(m.get("net", 0) or 0)
    except Exception:
        pass
    try:
        pos_data  = kite.positions()
        # day positions: opened today (MIS intraday + any NRML/CNC opened today)
        day_pos   = pos_data.get("day", [])
        net_pos   = pos_data.get("net", [])
        realised   = sum(float(p.get("realised", 0) or 0) for p in day_pos)
        unrealised = sum(float(p.get("unrealised", 0) or 0) for p in net_pos)
        info["day_realised"]   = round(realised, 2)
        info["day_unrealised"] = round(unrealised, 2)
        # build a compact summary list for the positions panel
        info["day_positions"] = [
            {
                "symbol":    p.get("tradingsymbol", ""),
                "product":   p.get("product", ""),
                "qty":       int(p.get("quantity", 0)),
                "avg_price": float(p.get("average_price", 0) or 0),
                "ltp":       float(p.get("last_price", 0) or 0),
                "pnl":       float(p.get("pnl", 0) or 0),
                "realised":  float(p.get("realised", 0) or 0),
                "unrealised":float(p.get("unrealised", 0) or 0),
            }
            for p in net_pos
            if float(p.get("pnl", 0) or 0) != 0 or int(p.get("quantity", 0)) != 0
        ]
    except Exception:
        pass
    return info


def render_account_banner(info: dict) -> None:
    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")

    # P&L colour helpers (pre-computed to avoid f-string nesting)
    _realised   = info["day_realised"]
    _unrealised = info["day_unrealised"]
    _total_pnl  = _realised + _unrealised
    _rc = "#a5d6a7" if _realised   >= 0 else "#ef9a9a"
    _uc = "#a5d6a7" if _unrealised >= 0 else "#ef9a9a"
    _tc = "#a5d6a7" if _total_pnl  >= 0 else "#ef9a9a"
    _nc = "#a5d6a7" if info["net"] >= 0 else "#ef9a9a"
    _r_sign  = "+" if _realised   >= 0 else ""
    _u_sign  = "+" if _unrealised >= 0 else ""
    _t_sign  = "+" if _total_pnl  >= 0 else ""

    st.markdown(
        f"""
        <div style='background:linear-gradient(90deg,#1a73e8,#0d47a1);
                    border-radius:10px;padding:16px 24px;margin-bottom:8px;
                    display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px'>
          <div>
            <span style='color:#fff;font-size:20px;font-weight:700'>
              👋 {greeting}, {info["name"]}!
            </span><br>
            <span style='color:#bbdefb;font-size:13px'>
              {datetime.now():%A, %d %B %Y &nbsp;·&nbsp; %H:%M:%S}
            </span>
          </div>
          <div style='display:flex;gap:24px;text-align:center;flex-wrap:wrap'>
            <div><div style='color:#bbdefb;font-size:11px;text-transform:uppercase'>Available Cash</div>
                 <div style='color:#a5d6a7;font-size:18px;font-weight:700'>₹{info["cash"]:,.0f}</div></div>
            <div><div style='color:#bbdefb;font-size:11px;text-transform:uppercase'>Margin Used</div>
                 <div style='color:#ef9a9a;font-size:18px;font-weight:700'>₹{info["used"]:,.0f}</div></div>
            <div><div style='color:#bbdefb;font-size:11px;text-transform:uppercase'>Net Balance</div>
                 <div style='color:{_nc};font-size:18px;font-weight:700'>₹{info["net"]:,.0f}</div></div>
            <div style='border-left:1px solid #3d6ed4;padding-left:24px'>
              <div style='color:#bbdefb;font-size:11px;text-transform:uppercase'>Day Booked P&amp;L</div>
              <div style='color:{_rc};font-size:18px;font-weight:700'>{_r_sign}₹{_realised:,.0f}</div>
            </div>
            <div><div style='color:#bbdefb;font-size:11px;text-transform:uppercase'>MTM (Open)</div>
                 <div style='color:{_uc};font-size:18px;font-weight:700'>{_u_sign}₹{_unrealised:,.0f}</div></div>
            <div><div style='color:#bbdefb;font-size:11px;text-transform:uppercase'>Total Day P&amp;L</div>
                 <div style='color:{_tc};font-size:20px;font-weight:800'>{_t_sign}₹{_total_pnl:,.0f}</div></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── live positions panel from Zerodha ─────────────────────────────────────
    day_pos = info.get("day_positions", [])
    if day_pos:
        with st.expander(
            f"📊 Zerodha Positions — {len(day_pos)} active  |  "
            f"Total P&L: {'+'if _total_pnl>=0 else ''}₹{_total_pnl:,.0f}",
            expanded=False,
        ):
            _rows = []
            for p in day_pos:
                _pnl  = p["pnl"]
                _real = p["realised"]
                _unrl = p["unrealised"]
                _qty  = p["qty"]
                _status = "OPEN" if _qty != 0 else "CLOSED"
                _pnl_col = "🟢" if _pnl >= 0 else "🔴"
                _rows.append({
                    "Symbol":      p["symbol"],
                    "Product":     p["product"],
                    "Qty":         _qty,
                    "Avg":         f"₹{p['avg_price']:.2f}",
                    "LTP":         f"₹{p['ltp']:.2f}",
                    "Status":      _status,
                    "Booked P&L":  f"{'+'if _real>=0 else ''}₹{_real:,.0f}",
                    "MTM":         f"{'+'if _unrl>=0 else ''}₹{_unrl:,.0f}",
                    "Total P&L":   f"{_pnl_col} {'+'if _pnl>=0 else ''}₹{_pnl:,.0f}",
                })
            pos_df = pd.DataFrame(_rows)
            st.dataframe(pos_df, use_container_width=True, hide_index=True)


# ── universe & data ───────────────────────────────────────────────────────────
@st.cache_data(ttl=86_400, show_spinner="Fetching Nifty 500 universe…")
def load_nifty500() -> list[str]:
    try:
        df  = pd.read_csv(NSE_N500_URL)
        col = next((c for c in df.columns if "symbol" in c.lower()), None)
        if col:
            return df[col].dropna().str.strip().tolist()
    except Exception as e:
        st.warning(f"Nifty 500 CSV failed ({e}). Using Nifty 100 fallback.")
    return [
        "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","ITC","SBIN",
        "BHARTIARTL","KOTAKBANK","AXISBANK","LT","BAJFINANCE","HCLTECH","ASIANPAINT",
        "MARUTI","TITAN","SUNPHARMA","ULTRACEMCO","NESTLEIND","WIPRO","POWERGRID",
        "NTPC","COALINDIA","ONGC","TATAMOTORS","TATASTEEL","JSWSTEEL","ADANIENT",
        "ADANIPORTS","TECHM","DRREDDY","CIPLA","DIVISLAB","BAJAJFINSV","BAJAJ-AUTO",
        "HEROMOTOCO","EICHERMOT","APOLLOHOSP","BRITANNIA","TATACONSUM","INDUSINDBK",
        "M&M","GRASIM","BPCL","IOC","HINDALCO","SHREECEM","PIDILITIND","HAVELLS",
        "DABUR","MARICO","COLPAL","GODREJCP","BERGEPAINT","VOLTAS","ABB","SIEMENS",
        "BOSCHLTD","CUMMINSIND","THERMAX","RECLTD","PFC","CANBK","BANKBARODA",
        "FEDERALBNK","IDFCFIRSTB","BANDHANBNK","RBLBANK","PNB","UNIONBANK","CONCOR",
        "IRCTC","HAL","BEL","ZOMATO","TATAPOWER","ADANIGREEN","TRENT","DMART",
        "PAGEIND","MUTHOOTFIN","CHOLAFIN","MANAPPURAM","IRFC","RVNL",
    ]


@st.cache_data(ttl=86_400, show_spinner="Fetching F&O universe…")
def load_fno_stocks() -> list[str]:
    """Load F&O stock universe.
    Strategy 1: Kite instruments NFO (most accurate, requires auth).
    Strategy 2: Hardcoded list of ~180 current F&O stocks.
    """
    # ── Strategy 1: Kite NFO instruments ─────────────────────────────────────
    try:
        kite = get_kite()
        inst = kite.instruments("NFO")
        # Keep only stock futures (not index futures like NIFTY/BANKNIFTY)
        # instrument_type == "FUT" and name not in index list
        _index = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
                  "SENSEX", "BANKEX"}
        syms = sorted({
            i["name"].strip().upper()
            for i in inst
            if i.get("instrument_type") == "FUT"
            and i.get("name", "").strip().upper() not in _index
        })
        if len(syms) > 50:   # sanity check — expect 150-200
            _log.info(f"F&O universe: {len(syms)} stocks from Kite NFO instruments")
            return syms
    except Exception as e:
        _log.warning(f"Kite NFO instruments failed: {e}. Using hardcoded fallback.")

    # ── Strategy 2: hardcoded list (as of Nov 2026) ───────────────────────────
    return sorted([
            "AARTIIND","ABB","ABBOTINDIA","ABCAPITAL","ABFRL","ACC","ADANIENT",
            "ADANIPORTS","ALKEM","AMBUJACEM","APOLLOHOSP","APOLLOTYRE","ASHOKLEY",
            "ASIANPAINT","ASTRAL","ATUL","AUBANK","AUROPHARMA","AXISBANK","BAJAJ-AUTO",
            "BAJAJFINSV","BAJFINANCE","BALKRISIND","BANDHANBNK","BANKBARODA",
            "BATAINDIA","BEL","BERGEPAINT","BHARTIARTL","BHEL","BIOCON","BOSCHLTD",
            "BPCL","BRITANNIA","BSOFT","CANBK","CANFINHOME","CHAMBLFERT","CHOLAFIN",
            "CIPLA","COALINDIA","COFORGE","COLPAL","CONCOR","COROMANDEL","CROMPTON",
            "CUB","CUMMINSIND","DABUR","DALBHARAT","DEEPAKNTR","DELTACORP","DIVISLAB",
            "DIXON","DLF","DRREDDY","EICHERMOT","ESCORTS","EXIDEIND","FEDERALBNK",
            "FINNIFTY","FORTIS","GAIL","GLENMARK","GMRINFRA","GNFC","GODREJCP",
            "GODREJPROP","GRANULES","GRAPHITE","GRASIM","GSPL","GUJARATGAS","HAL",
            "HAVELLS","HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO","HINDALCO",
            "HINDCOPPER","HINDPETRO","HINDUNILVR","ICICIBANK","ICICIGI","ICICIPRULI",
            "IDEA","IDFCFIRSTB","IEX","IGL","INDHOTEL","INDIACEM","INDIAMART",
            "INDUSINDBK","INDUSTOWER","INFY","IOC","IPCALAB","IRCTC","ITC",
            "JINDALSTEL","JKCEMENT","JSWSTEEL","JUBLFOOD","KOTAKBANK","L&TFH",
            "LALPATHLAB","LAURUSLABS","LICHSGFIN","LT","LTIM","LTTS","LUPIN",
            "M&M","M&MFIN","MANAPPURAM","MARICO","MARUTI","MCX","METROPOLIS",
            "MFSL","MGL","MOTHERSON","MPHASIS","MRF","MUTHOOTFIN","NAM-INDIA",
            "NATIONALUM","NAUKRI","NAVINFLUOR","NESTLEIND","NMDC","NTPC","OBEROIRLTY",
            "OFSS","ONGC","PAGEIND","PEL","PERSISTENT","PETRONET","PFC","PIDILITIND",
            "PIIND","PNB","POLYCAB","POWERGRID","PVRINOX","RAIN","RBLBANK",
            "RECLTD","RELIANCE","SAIL","SBICARD","SBILIFE","SBIN","SHREECEM",
            "SIEMENS","SRF","SUNPHARMA","SUNTV","SYNGENE","TATACHEM","TATACOMM",
            "TATACONSUM","TATAMOTORS","TATAPOWER","TATASTEEL","TCS","TECHM","TITAN",
            "TORNTPHARM","TRENT","TVSMOTOR","UBL","ULTRACEMCO","UPL","VEDL",
            "VOLTAS","WIPRO","ZEEL","ZOMATO",
        ])


@st.cache_data(ttl=86_400, show_spinner="Fetching All NSE Cash universe…")
def load_all_nse_cash() -> list[str]:
    try:
        df  = pd.read_csv(NSE_EQ_URL)
        # NSE EQUITY_L.csv columns: SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, …
        sym_col    = next((c for c in df.columns if c.strip().upper() == "SYMBOL"), None)
        series_col = next((c for c in df.columns if "series" in c.lower()), None)
        if sym_col is None:
            raise ValueError("SYMBOL column not found in EQUITY_L.csv")
        if series_col:
            df = df[df[series_col].str.strip() == "EQ"]
        return df[sym_col].dropna().str.strip().tolist()
    except Exception as e:
        st.warning(f"All NSE Cash CSV failed ({e}). Falling back to Nifty 500.")
        return load_nifty500()


@st.cache_data(ttl=3_600, show_spinner="Loading futures contracts…")
def load_futures_instruments(month: str = "current") -> dict[str, dict]:
    """Return {symbol: {lot_size, tradingsymbol, expiry, nfo_key}} for stock futures.

    nfo_key = "NFO:tradingsymbol" — ready for kite.quote()
    month = "current" | "next"
    """
    import datetime as _dt
    _index = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
              "SENSEX", "BANKEX"}
    try:
        kite  = get_kite()
        inst  = kite.instruments("NFO")
        today = _dt.date.today()
        expiries = sorted({
            _dt.date.fromisoformat(str(i["expiry"]))
            for i in inst
            if i.get("instrument_type") == "FUT"
            and i.get("name", "").strip().upper() not in _index
            and i.get("expiry")
        })
        if not expiries:
            return {}
        future_expiries = [e for e in expiries if e >= today]
        if not future_expiries:
            return {}
        target_expiry = future_expiries[0] if month == "current" else (
            future_expiries[1] if len(future_expiries) > 1 else future_expiries[0]
        )
        result: dict[str, dict] = {}
        for i in inst:
            if i.get("instrument_type") != "FUT":
                continue
            name = i.get("name", "").strip().upper()
            if name in _index:
                continue
            try:
                exp = _dt.date.fromisoformat(str(i["expiry"]))
            except Exception:
                continue
            if exp == target_expiry:
                ts = i.get("tradingsymbol", "").strip()
                result[name] = {
                    "lot_size":      int(i.get("lot_size") or 1),
                    "tradingsymbol": ts,
                    "expiry":        str(target_expiry),
                    "nfo_key":       f"NFO:{ts}",
                }
        _log.info(f"Futures {month}: {len(result)} stocks, expiry {target_expiry}")
        return result
    except Exception as e:
        _log.warning(f"load_futures_instruments failed: {e}")
        return {}


def load_futures_lot_sizes(month: str = "current") -> dict[str, int]:
    """Convenience wrapper — returns {sym: lot_size} only."""
    return {s: v["lot_size"] for s, v in load_futures_instruments(month).items()}


def load_universe(choice: str = "Nifty 500") -> list[str]:
    if choice == "All NSE Cash":
        return load_all_nse_cash()
    if choice == "Nifty F&O":
        return load_fno_stocks()
    if choice in ("Futures – Current Month", "Futures – Next Month"):
        month = "current" if "Current" in choice else "next"
        lots  = load_futures_lot_sizes(month)
        return sorted(lots.keys()) if lots else load_fno_stocks()
    return load_nifty500()


def _fetch_fund(sym: str) -> dict | None:
    try:
        info = yf.Ticker(sym + ".NS").info
        mkt  = info.get("marketCap") or 0
        return {
            "symbol":        sym,
            "name":          info.get("longName") or info.get("shortName") or sym,
            "market_cap_cr": mkt / 1e7,
            "pe":            info.get("trailingPE"),
            "eps":           info.get("trailingEps"),
            "inst_pct":      (info.get("heldPercentInstitutions") or 0) * 100,
        }
    except Exception:
        return None


@st.cache_data(ttl=86_400, show_spinner=False)
def load_fundamentals(universe: tuple) -> pd.DataFrame:
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_FUND_WORKERS) as ex:
        for r in ex.map(_fetch_fund, universe):
            if r:
                rows.append(r)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fundamental_filter(df: pd.DataFrame, enabled: bool = True) -> tuple[pd.DataFrame, dict]:
    funnel = {"total": len(df)}
    if df.empty or not enabled:
        for k in ("after_mktcap", "after_pe", "after_eps", "after_inst"):
            funnel[k] = len(df)
        return df, funnel
    df = df[df["market_cap_cr"] >= MARKET_CAP_MIN_CR]; funnel["after_mktcap"] = len(df)
    df = df[df["pe"].notna() & (df["pe"] > 0) & (df["pe"] <= PE_MAX)]; funnel["after_pe"] = len(df)
    df = df[df["eps"].notna() & (df["eps"] >= EPS_MIN)]; funnel["after_eps"] = len(df)
    df = df[df["inst_pct"] >= INST_MIN_PCT]; funnel["after_inst"] = len(df)
    return df, funnel


def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    closes = closes.dropna()
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-9)
    rsi   = 100 - (100 / (1 + rs))
    val   = rsi.iloc[-1]
    return float(val) if pd.notna(val) else 50.0


@st.cache_data(ttl=300)   # cache 5 min
def get_15min_indicators(sym: str) -> dict:
    """Fetch today's 15-min bars: RSI, VWAP, and Volume Delta (buying pressure)."""
    _default = {"rsi_15": 50.0, "vwap_15": 0.0, "above_vwap_15": None,
                "buy_vol_pct": 50.0, "vol_delta": 0.0}
    sym_yf = sym + ".NS"
    try:
        df = yf.Ticker(sym_yf).history(period="1d", interval="15m", auto_adjust=True)
        if df is None or df.empty or len(df) < 5:
            return _default
        close  = df["Close"].astype(float)
        open_  = df["Open"].astype(float)
        volume = df["Volume"].astype(float)
        high   = df["High"].astype(float)
        low    = df["Low"].astype(float)

        # intraday cumulative VWAP
        tp   = (high + low + close) / 3
        cvol = volume.cumsum()
        vwap = float((tp * volume).cumsum().iloc[-1] / cvol.iloc[-1]) if cvol.iloc[-1] > 0 else 0.0

        rsi = _calc_rsi(close, 14)
        ltp = float(close.iloc[-1])

        # Volume Delta — candles where close > open = buying volume
        # Uses actual traded volume, not pending orders → reliable signal
        buy_mask  = close > open_
        sell_mask = close < open_
        buy_vol   = float(volume[buy_mask].sum())
        sell_vol  = float(volume[sell_mask].sum())
        total_vol = buy_vol + sell_vol
        buy_vol_pct = round(buy_vol / total_vol * 100, 1) if total_vol > 0 else 50.0
        vol_delta   = round((buy_vol - sell_vol) / total_vol * 100, 1) if total_vol > 0 else 0.0

        return {"rsi_15": round(rsi, 1), "vwap_15": round(vwap, 2),
                "above_vwap_15": ltp >= vwap,
                "buy_vol_pct": buy_vol_pct,   # e.g. 68 = 68% of volume was buying
                "vol_delta": vol_delta}        # e.g. +36 = net 36% more buying than selling
    except Exception as e:
        _log.warning(f"15min indicators fetch failed for {sym}: {e}")
        return _default


def _calc_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    """Wilder-smoothed ADX. Returns 20.0 on insufficient data."""
    try:
        high, low, close = high.dropna(), low.dropna(), close.dropna()
        n = min(len(high), len(low), len(close))
        if n < period * 2:
            return 20.0
        high, low, close = high.iloc[-n:], low.iloc[-n:], close.iloc[-n:]
        tr   = pd.concat([high - low,
                          (high - close.shift()).abs(),
                          (low  - close.shift()).abs()], axis=1).max(axis=1)
        dmp  = (high.diff()).clip(lower=0)
        dmm  = (-low.diff()).clip(lower=0)
        dmp  = dmp.where(dmp > dmm, 0.0)
        dmm  = dmm.where(dmm > dmp, 0.0)
        atr14  = tr.ewm(alpha=1/period,  adjust=False).mean()
        dip14  = 100 * dmp.ewm(alpha=1/period, adjust=False).mean() / atr14.replace(0, 1e-9)
        dim14  = 100 * dmm.ewm(alpha=1/period, adjust=False).mean() / atr14.replace(0, 1e-9)
        dx     = 100 * (dip14 - dim14).abs() / (dip14 + dim14).replace(0, 1e-9)
        adx    = dx.ewm(alpha=1/period, adjust=False).mean()
        val    = adx.iloc[-1]
        return float(val) if pd.notna(val) else 20.0
    except Exception:
        return 20.0


def _bb_squeeze(closes: pd.Series, period: int = 20, threshold: float = 0.70) -> bool:
    """True when current BB width is below threshold × its own rolling average (compression)."""
    closes = closes.dropna()
    if len(closes) < period + 5:
        return False
    mid   = closes.rolling(period).mean()
    std   = closes.rolling(period).std()
    width = (2 * std) / mid          # BB width as fraction of price
    cur   = float(width.iloc[-1])
    avg   = float(width.iloc[-period:].mean())
    return pd.notna(cur) and pd.notna(avg) and avg > 0 and cur < avg * threshold


def _nr7(highs: pd.Series, lows: pd.Series) -> bool:
    """True when today's high-low range is the narrowest in the last 7 candles."""
    highs = highs.dropna()
    lows  = lows.dropna()
    n     = min(len(highs), len(lows), 7)
    if n < 2:
        return False
    ranges    = (highs - lows).iloc[-n:]
    today_rng = float(ranges.iloc[-1])
    return pd.notna(today_rng) and today_rng == float(ranges.min())


@st.cache_data(ttl=86_400, show_spinner=False)
def load_daily_data(symbols: tuple) -> dict[str, dict]:
    """Returns {sym: {avg_vol, rsi, bb_squeeze, nr7, ema21/50/200, ...}} cached for the day."""
    syms_yf = [s + ".NS" for s in symbols]
    try:
        # 1y gives ~252 trading days — enough for 200-day EMA
        raw = yf.download(syms_yf, period="1y", interval="1d",
                          progress=False, auto_adjust=True)
    except Exception:
        return {}
    result: dict[str, dict] = {}
    single = len(syms_yf) == 1
    for sym, sym_yf in zip(symbols, syms_yf):
        try:
            vol   = raw["Volume"] if single else raw["Volume"][sym_yf]
            avg   = float(vol.iloc[-21:-1].mean())
            if avg <= 0:
                continue
            close  = (raw["Close"] if single else raw["Close"][sym_yf]).dropna()
            high_s = (raw["High"]  if single else raw["High"][sym_yf]).dropna()
            low_s  = (raw["Low"]   if single else raw["Low"][sym_yf]).dropna()

            # ── EMA 21 / 50 / 200 ────────────────────────────────────────────
            ltp_today = float(close.iloc[-1])
            ema21  = float(close.ewm(span=21,  adjust=False).mean().iloc[-1])
            ema50  = float(close.ewm(span=50,  adjust=False).mean().iloc[-1])
            ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
            above_ema21  = ltp_today > ema21
            above_ema50  = ltp_today > ema50
            above_ema200 = ltp_today > ema200

            # ── inside bar: today's range is inside yesterday's range ─────────
            inside_bar = (len(high_s) >= 2 and len(low_s) >= 2 and
                          float(high_s.iloc[-1]) < float(high_s.iloc[-2]) and
                          float(low_s.iloc[-1])  > float(low_s.iloc[-2]))

            # ── range breakout: today's close > 5-day high (excl. today) ────
            range_bo = (len(high_s) >= 6 and
                        float(close.iloc[-1]) >= float(high_s.iloc[-6:-1].max()))

            # ── VCP: 3 successive range contractions ─────────────────────────
            ranges = (high_s - low_s).dropna()
            if len(ranges) >= 15:
                r_now  = float(ranges.iloc[-3:].mean())
                r_mid  = float(ranges.iloc[-8:-3].mean())
                r_base = float(ranges.iloc[-15:-8].mean())
                vcp    = (r_now < r_mid * 0.90 and r_mid < r_base * 0.90
                          and r_base > 0 and r_mid > 0)
            else:
                vcp = False

            # ── MACD bullish (12 EMA > 26 EMA after crossover) ──────────────
            ema12        = close.ewm(span=12, adjust=False).mean()
            ema26        = close.ewm(span=26, adjust=False).mean()
            macd_line    = ema12 - ema26
            macd_signal  = macd_line.ewm(span=9, adjust=False).mean()
            macd_bullish = bool(macd_line.iloc[-1] > macd_signal.iloc[-1])

            # ── ADX(14) — trend strength ────────────────────────────────────
            adx_val = _calc_adx(high_s, low_s, close, 14)

            # ── consecutive up-days (last 3) ─────────────────────────────────
            up_days = int((close.diff().iloc[-3:] > 0).sum())

            # ── 52-week High / Low from yfinance (last 252 trading days) ────
            high_252 = float(high_s.iloc[-252:].max()) if len(high_s) >= 1 else ltp_today
            low_252  = float(low_s.iloc[-252:].min())  if len(low_s)  >= 1 else ltp_today

            # ── Volume Delta: count up/down candles in last 10 days ──────────
            _open_s  = (raw["Open"] if single else raw["Open"][sym_yf]).dropna()
            _last10c = close.iloc[-11:-1]   # last 10 completed days (excl. today)
            _last10o = _open_s.iloc[-11:-1]
            _up_mask  = _last10c > _last10o
            buy_days  = int(_up_mask.sum())
            sell_days = int((~_up_mask).sum())

            result[sym] = {
                "avg_vol":     avg,
                "rsi":         _calc_rsi(close, 14),
                "bb_squeeze":  _bb_squeeze(close),
                "nr7":         _nr7(high_s, low_s),
                "inside_bar":  inside_bar,
                "range_bo":    range_bo,
                "vcp":         vcp,
                "ema21":       round(ema21,  2),
                "ema50":       round(ema50,  2),
                "ema200":      round(ema200, 2),
                "above_ema21":  above_ema21,
                "above_ema50":  above_ema50,
                "above_ema200": above_ema200,
                "macd_bullish": macd_bullish,
                "adx":          round(adx_val, 1),
                "up_days":      up_days,
                "high_52w_yf":  round(high_252, 2),
                "low_52w_yf":   round(low_252,  2),
                "buy_days":     buy_days,
                "sell_days":    sell_days,
            }
        except Exception:
            pass
    return result


def _gnews_headlines(sym: str, company: str = "") -> list[tuple[str, str]]:
    """Fetch up to 8 (title, url) pairs from Google News RSS for a stock symbol."""
    import urllib.parse
    from bs4 import BeautifulSoup
    query = f"{company} {sym} NSE stock" if company else f"{sym} NSE stock India"
    url   = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"
    })
    try:
        resp = _requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if not resp.ok:
            return []
        soup  = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")[:8]
        results = []
        for i in items:
            title_tag = i.find("title")
            link_tag  = i.find("link")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            # Google News RSS <link> is a redirect URL — use it directly
            link  = link_tag.get_text(strip=True) if link_tag else ""
            results.append((title, link))
        return results
    except Exception:
        return []


def _screener_news(sym: str) -> tuple[list[tuple[str, str]], bool]:
    """Scrape latest announcements from Screener.in. Returns ([(title, url)], has_results)."""
    from bs4 import BeautifulSoup
    base_url   = f"https://www.screener.in/company/{sym}/consolidated/"
    _result_kw = {"result", "results", "quarterly", "q1", "q2", "q3", "q4",
                  "earnings", "profit", "revenue", "announcement"}
    try:
        resp = _requests.get(base_url, timeout=7,
                             headers={"User-Agent": "Mozilla/5.0",
                                      "Accept-Language": "en-US,en;q=0.9"})
        if not resp.ok:
            return [], False
        soup    = BeautifulSoup(resp.text, "html.parser")
        section = soup.find(id="announcements")
        if not section:
            return [], False
        results, has_results = [], False
        for li in section.find_all("li")[:6]:
            a_tag = li.find("a", href=True)
            text  = (a_tag.get_text(strip=True) if a_tag
                     else li.get_text(separator=" ", strip=True))[:120]
            href  = a_tag["href"] if a_tag else base_url
            # make relative URLs absolute
            if href.startswith("/"):
                href = "https://www.screener.in" + href
            if text:
                results.append((text, href))
                if any(kw in text.lower() for kw in _result_kw):
                    has_results = True
        return results, has_results
    except Exception:
        return [], False


def _fetch_news_one(sym: str) -> tuple[str, dict]:
    """Fetch news from Google News RSS + Screener.in; score sentiment with TextBlob."""
    try:
        from textblob import TextBlob
        _result_kw = {"result", "results", "quarterly", "q1", "q2", "q3", "q4",
                      "earnings", "profit", "revenue"}

        # 1. Google News RSS — primary source, free, no API key
        gnews: list[tuple[str, str]] = _gnews_headlines(sym)

        # 2. Screener.in announcements — quarterly results flag + BSE/NSE filings
        sc_items, has_results = _screener_news(sym)

        # 3. yfinance as supplementary fallback if both above are empty
        yf_items: list[tuple[str, str]] = []
        if not gnews and not sc_items:
            try:
                ticker   = yf.Ticker(sym + ".NS")
                articles = ticker.news or []
                yf_items = [(a.get("title", ""), a.get("link", ""))
                            for a in articles[:5] if a.get("title")]
            except Exception:
                pass

        all_items: list[tuple[str, str]] = gnews + sc_items + yf_items
        scores, headlines, urls = [], [], []
        for title, url in all_items[:10]:
            if not title:
                continue
            scores.append(TextBlob(title).sentiment.polarity)
            headlines.append(title)
            urls.append(url)
            if any(kw in title.lower() for kw in _result_kw):
                has_results = True

        avg = sum(scores) / len(scores) if scores else 0.0
        return sym, {"sentiment":   round(avg, 3),
                     "headlines":   headlines[:3],
                     "urls":        urls[:3],
                     "has_results": has_results,
                     "n_articles":  len(all_items),
                     "sources":     (["gnews"]    if gnews    else [])
                                  + (["screener"] if sc_items else [])
                                  + (["yfinance"] if yf_items else [])}
    except Exception:
        return sym, {}


@st.cache_data(ttl=7_200, show_spinner=False)   # cache 2 h — news refreshes slowly
def load_news_data(symbols: tuple, _v: int = 2) -> dict[str, dict]:
    """Returns {sym: {sentiment, headlines, urls, has_results, n_articles}} for all symbols."""
    result: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_FUND_WORKERS) as ex:
        for sym, data in ex.map(_fetch_news_one, symbols):
            if data:
                result[sym] = data
    return result


def load_kite_quotes(kite: KiteConnect, symbols: list[str],
                     fut_instruments: dict[str, dict] | None = None) -> dict[str, dict]:
    """Fetch quotes. For futures-mode symbols uses NFO:tradingsymbol; rest use NSE:sym."""
    result: dict[str, dict] = {}
    fut = fut_instruments or {}

    # split into futures (NFO) and cash (NSE)
    nfo_syms  = [(s, fut[s]["nfo_key"]) for s in symbols if s in fut]
    cash_syms = [s for s in symbols if s not in fut]

    # fetch futures quotes
    for i in range(0, len(nfo_syms), 500):
        batch_map = {nfo_key: sym for sym, nfo_key in nfo_syms[i:i + 500]}
        try:
            data = kite.quote(list(batch_map.keys()))
            for nfo_key, val in data.items():
                result[batch_map[nfo_key]] = val
        except Exception as e:
            st.warning(f"Kite NFO quote error: {e}")

    # fetch cash equity quotes
    for i in range(0, len(cash_syms), 500):
        batch = [f"NSE:{s}" for s in cash_syms[i:i + 500]]
        try:
            data = kite.quote(batch)
            for key, val in data.items():
                result[key.replace("NSE:", "")] = val
        except Exception as e:
            st.warning(f"Kite quote error: {e}")

    return result


@st.cache_data(ttl=300, show_spinner=False)   # 5-min cache — refreshes with each candle
def load_15min_vwap(symbols: tuple) -> dict[str, dict]:
    """
    Download today's 15-min bars for all symbols in one shot and compute
    cumulative VWAP (sum(TP×Vol) / sum(Vol)).  Returns:
        {sym: {"vwap_15": float, "above_vwap_15": bool}}
    Missing symbols get {"vwap_15": 0.0, "above_vwap_15": None}.
    """
    if not symbols:
        return {}
    syms_yf = [s + ".NS" for s in symbols]
    result: dict[str, dict] = {}
    _default = {"vwap_15": 0.0, "above_vwap_15": None}
    try:
        raw = yf.download(syms_yf, period="1d", interval="15m",
                          progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return {s: _default for s in symbols}
        single = len(syms_yf) == 1
        for sym, sym_yf in zip(symbols, syms_yf):
            try:
                close  = (raw["Close"]  if single else raw["Close"][sym_yf]).dropna()
                high   = (raw["High"]   if single else raw["High"][sym_yf]).dropna()
                low    = (raw["Low"]    if single else raw["Low"][sym_yf]).dropna()
                volume = (raw["Volume"] if single else raw["Volume"][sym_yf]).dropna()
                if len(close) < 3:
                    result[sym] = _default
                    continue
                tp   = (high + low + close) / 3
                cvol = volume.cumsum()
                last_cvol = float(cvol.iloc[-1])
                vwap_15 = round(float((tp * volume).cumsum().iloc[-1] / last_cvol), 2) \
                          if last_cvol > 0 else 0.0
                ltp = float(close.iloc[-1])
                result[sym] = {
                    "vwap_15":      vwap_15,
                    "above_vwap_15": ltp >= vwap_15 if vwap_15 > 0 else None,
                }
            except Exception:
                result[sym] = _default
    except Exception as e:
        _log.warning(f"15-min VWAP batch fetch failed: {e}")
        return {s: _default for s in symbols}
    return result


def build_intraday_df(quotes: dict[str, dict], daily_data: dict[str, dict],
                      news_data: dict[str, dict] | None = None,
                      vwap_15_data: dict[str, dict] | None = None) -> pd.DataFrame:
    rows = []
    for sym, q in quotes.items():
        try:
            ltp        = float(q["last_price"])
            ohlc       = q.get("ohlc", {})
            prev_close = float(ohlc.get("close", 0))
            volume     = float(q.get("volume", 0))
            d          = daily_data.get(sym, {})
            avg_vol    = d.get("avg_vol", 0)
            rsi        = d.get("rsi", 50.0)
            if prev_close <= 0:
                continue
            pct_change  = (ltp - prev_close) / prev_close * 100
            # vol_surge: 1.0 (neutral) when daily baseline unavailable — stock still shown
            # so pattern-only strategies (VCP, Inside Bar, Swing) work even if yfinance fails
            vol_surge   = (volume / avg_vol) if avg_vol > 0 else 1.0
            # prefer yfinance 52W values (accurate); fall back to Kite quote if missing
            _yf_h52 = d.get("high_52w_yf", 0.0)
            _yf_l52 = d.get("low_52w_yf",  0.0)
            _kt_h52 = float(q.get("week_52_high") or 0)
            _kt_l52 = float(q.get("week_52_low")  or 0)
            high_52w = _yf_h52 if _yf_h52 > 0 else (_kt_h52 if _kt_h52 > 0 else ltp)
            low_52w  = _yf_l52 if _yf_l52 > 0 else (_kt_l52 if _kt_l52 > 0 else ltp)
            # sanity: high must be >= low
            if high_52w < low_52w:
                high_52w, low_52w = low_52w, high_52w
            dist_52w = (high_52w - ltp) / ltp * 100 if high_52w > 0 else 0.0
            # Use Kite's actual circuit limits (vary per stock: 2/5/10/20%)
            # F&O stocks have no circuit — Kite returns 0 in that case
            _uc = float(q.get("upper_circuit_limit") or 0)
            _lc = float(q.get("lower_circuit_limit") or 0)
            if _uc <= 0:
                _uc = prev_close * 1.20   # fallback: assume 20% if not provided
            if _lc <= 0:
                _lc = prev_close * 0.80
            dist_to_uc = (_uc - ltp) / ltp * 100 if ltp > 0 else 0.0
            dist_to_lc = (ltp - _lc) / ltp * 100 if ltp > 0 else 0.0
            day_high   = float(ohlc.get("high", ltp))
            day_low    = float(ohlc.get("low",  ltp))
            # 15-min cumulative VWAP (preferred); fall back to OHLC/4 when unavailable
            _vd        = (vwap_15_data or {}).get(sym, {})
            vwap_15    = float(_vd.get("vwap_15", 0.0) or 0.0)
            if vwap_15 > 0:
                vwap       = vwap_15
                above_vwap = ltp >= vwap
            else:
                vwap       = (float(ohlc.get("open", ltp)) + day_high + day_low + ltp) / 4
                above_vwap = ltp >= vwap
            near_day_high  = day_high > 0 and (day_high - ltp) / day_high * 100 <= 0.5
            intraday_range = (day_high - day_low) / prev_close * 100 if prev_close > 0 else 99.0
            bb_squeeze   = d.get("bb_squeeze",   False)
            nr7          = d.get("nr7",          False)
            inside_bar   = d.get("inside_bar",   False)
            range_bo     = d.get("range_bo",     False)
            vcp          = d.get("vcp",          False)
            above_ema21  = d.get("above_ema21",  False)
            above_ema50  = d.get("above_ema50",  False)
            above_ema200 = d.get("above_ema200", False)
            ema21        = d.get("ema21",  0.0)
            ema50        = d.get("ema50",  0.0)
            ema200       = d.get("ema200", 0.0)
            macd_bullish = d.get("macd_bullish", False)
            adx          = d.get("adx",          20.0)
            up_days      = d.get("up_days",       0)
            # ── Volume Delta: buying pressure from actual traded candles ─────
            # Today: use Kite's intraday OHLC to decide if today is a buying candle
            # + use last 5 daily candles from yfinance for a more robust signal
            _d_closes = []  # will use daily close vs open from yfinance
            _yf_d = daily_data.get(sym, {})
            # daily up/down candle ratio from yfinance (last 10 days, cached)
            _buy_days  = int(_yf_d.get("buy_days",  5))
            _sell_days = int(_yf_d.get("sell_days", 5))
            _today_up  = ltp > float(ohlc.get("open", ltp))
            # weight today's signal 2× vs each historical day
            _total_w = _buy_days + _sell_days + 2
            _buy_w   = _buy_days + (2 if _today_up else 0)
            bs_ratio = round(_buy_w / _total_w, 2) if _total_w > 0 else 0.5
            # news / results
            nd               = (news_data or {}).get(sym, {})
            news_sentiment   = nd.get("sentiment",    0.0)
            news_has_results = nd.get("has_results",  False)
            news_headlines   = nd.get("headlines",    [])
            rows.append({"symbol": sym, "ltp": ltp, "prev_close": prev_close,
                         "pct_change": pct_change, "volume": volume, "avg_vol_20": avg_vol,
                         "vol_surge": vol_surge, "dist_to_uc": round(dist_to_uc, 2),
                         "dist_to_lc": round(dist_to_lc, 2),
                         "upper_circuit": round(_uc, 2), "lower_circuit": round(_lc, 2),
                         "high_52w": round(high_52w, 2), "low_52w": round(low_52w, 2), "dist_52w": dist_52w,
                         "vwap": round(vwap, 2), "above_vwap": above_vwap, "rsi": round(rsi, 1),
                         "bb_squeeze": bb_squeeze, "nr7": nr7,
                         "near_day_high": near_day_high, "intraday_range": round(intraday_range, 2),
                         "inside_bar": inside_bar, "range_bo": range_bo, "vcp": vcp,
                         "above_ema21": above_ema21, "above_ema50": above_ema50,
                         "above_ema200": above_ema200,
                         "ema21": ema21, "ema50": ema50, "ema200": ema200,
                         "macd_bullish": macd_bullish, "adx": adx, "up_days": up_days,
                         "bs_ratio": bs_ratio,
                         "news_sentiment": round(news_sentiment, 3),
                         "news_has_results": news_has_results,
                         "news_headlines": news_headlines})
        except Exception:
            continue
    return pd.DataFrame(rows)


def technical_filter(df: pd.DataFrame,
                     use_pct: bool  = True,
                     min_pct: float = 2.0,
                     use_vol: bool  = True,
                     vol_min: float = 1.5,
                     use_rsi: bool  = False,
                     rsi_min: float = 30.0,
                     rsi_max: float = 70.0,
                     use_vwap: bool = False,
                     vwap_above: bool = True,
                     use_bs: bool    = False,
                     bs_min: float   = 1.5,
                     use_bb_squeeze: bool     = False,
                     use_nr7: bool            = False,
                     use_near_day_high: bool  = False,
                     use_tight_intraday: bool = False,
                     use_setup_rsi: bool      = False,
                     use_inside_bar: bool     = False,
                     use_range_bo: bool       = False,
                     use_vcp: bool            = False,
                     use_news: bool           = False,
                     news_results_only: bool  = False,
                     use_ema21: bool          = False,
                     use_ema50: bool          = False,
                     use_ema200: bool         = False) -> pd.DataFrame:
    if df.empty:
        return df

    # ── momentum filters (AND with each other) ────────────────────────────────
    if use_pct:
        df = df[df["pct_change"] >= min_pct]
    if use_vol:
        df = df[df["vol_surge"]  >= vol_min]
    if use_rsi and "rsi" in df.columns:
        df = df[(df["rsi"] >= rsi_min) & (df["rsi"] <= rsi_max)]
    if use_vwap and "above_vwap" in df.columns:
        df = df[df["above_vwap"] == vwap_above]
    if use_bs and "bs_ratio" in df.columns:
        df = df[df["bs_ratio"] >= bs_min]

    # ── strategy / setup filters — OR logic among themselves ─────────────────
    # Each enabled strategy contributes stocks; a stock passes if it matches
    # ANY of the enabled strategies. This lets VCP + Inside Bar show stocks
    # that fit either pattern, not just both at once.
    strategy_masks = []
    _col = lambda c: c in df.columns   # shorthand

    if use_bb_squeeze    and _col("bb_squeeze"):    strategy_masks.append(df["bb_squeeze"])
    if use_nr7           and _col("nr7"):           strategy_masks.append(df["nr7"])
    if use_near_day_high and _col("near_day_high"): strategy_masks.append(df["near_day_high"])
    if use_tight_intraday and _col("intraday_range"):
        strategy_masks.append(df["intraday_range"] <= 1.5)
    if use_setup_rsi and _col("rsi"):
        strategy_masks.append((df["rsi"] >= 45) & (df["rsi"] <= 60))
    if use_inside_bar and _col("inside_bar"):       strategy_masks.append(df["inside_bar"])
    if use_range_bo   and _col("range_bo"):         strategy_masks.append(df["range_bo"])
    if use_vcp        and _col("vcp"):              strategy_masks.append(df["vcp"])

    if strategy_masks:
        combined = strategy_masks[0]
        for m in strategy_masks[1:]:
            combined = combined | m
        df = df[combined]

    # ── EMA filters (AND — each enabled EMA must be satisfied) ──────────────
    if use_ema21  and _col("above_ema21"):  df = df[df["above_ema21"]  == True]  # noqa: E712
    if use_ema50  and _col("above_ema50"):  df = df[df["above_ema50"]  == True]  # noqa: E712
    if use_ema200 and _col("above_ema200"): df = df[df["above_ema200"] == True]  # noqa: E712

    # ── news / results filter (AND — applied on top of everything) ───────────
    if use_news and _col("news_sentiment"):
        df = df[df["news_sentiment"] > 0.05]
    if news_results_only and _col("news_has_results"):
        df = df[df["news_has_results"] == True]       # noqa: E712

    return df


def score_df(df: pd.DataFrame) -> pd.DataFrame:
    """Compute intraday score AND swing_score for every row."""
    if df.empty:
        return df

    # ── existing intraday score ───────────────────────────────────────────────
    max_surge = max(df["vol_surge"].max(), 1)
    max_pct   = max(df["pct_change"].max(), 1)
    max_52w, min_52w = df["dist_52w"].max(), df["dist_52w"].min()
    df["vol_score"]  = (df["vol_surge"]  / max_surge * 40).clip(0, 40)
    df["mom_score"]  = (df["pct_change"] / max_pct   * 40).clip(0, 40)
    df["res_score"]  = (((max_52w - df["dist_52w"]) / (max_52w - min_52w) * 20).clip(0, 20)
                        if max_52w > min_52w else 10.0)
    setup_pts = pd.Series(0.0, index=df.index)
    for col in ("bb_squeeze", "nr7", "near_day_high", "inside_bar", "range_bo", "vcp"):
        if col in df.columns:
            setup_pts += df[col].astype(float) * 5
    if "intraday_range" in df.columns:
        setup_pts += (df["intraday_range"] <= 1.5).astype(float) * 5
    if "rsi" in df.columns:
        setup_pts += ((df["rsi"] >= 45) & (df["rsi"] <= 60)).astype(float) * 5
    df["setup_score"] = setup_pts.clip(0, 25).round(0).astype(int)
    df["score"] = (df["vol_score"] + df["mom_score"] + df["res_score"] + setup_pts).round(1)

    # ── swing_score: 3-5 day high-probability setup ───────────────────────────
    # Weights: EMA alignment 30 | RSI quality 25 | Volume 20 | Near 52W High 15 | ADX 10
    sw = pd.Series(0.0, index=df.index)

    # 1. EMA trend alignment (30 pts)
    _c = lambda col: col in df.columns
    if _c("above_ema200"): sw += df["above_ema200"].astype(float) * 10
    if _c("above_ema50"):  sw += df["above_ema50"].astype(float)  * 10
    if _c("above_ema21"):  sw += df["above_ema21"].astype(float)  * 5
    if _c("above_ema21") and _c("above_ema50") and _c("above_ema200"):
        fully_aligned = df["above_ema21"] & df["above_ema50"] & df["above_ema200"]
        sw += fully_aligned.astype(float) * 5   # bonus for all 3 aligned

    # 2. RSI quality (25 pts) — 50-65 is the sweet spot for swing entries
    if _c("rsi"):
        rsi = df["rsi"]
        sw += ((rsi >= 50) & (rsi <= 65)).astype(float) * 25
        sw += ((rsi >= 65) & (rsi <= 72)).astype(float) * 18
        sw += ((rsi >= 45) & (rsi <  50)).astype(float) * 12
        sw += ((rsi >= 72) & (rsi <= 80)).astype(float) * 8

    # 3. Volume conviction (20 pts)
    if _c("vol_surge"):
        vs = df["vol_surge"]
        sw += (vs >= 2.0).astype(float) * 20
        sw += ((vs >= 1.5) & (vs < 2.0)).astype(float) * 12
        sw += ((vs >= 1.2) & (vs < 1.5)).astype(float) * 6

    # 4. Proximity to 52W high (15 pts) — strong stocks near highs
    if _c("dist_52w"):
        dist = df["dist_52w"]
        sw += (dist <= 5).astype(float)                       * 15
        sw += ((dist > 5)  & (dist <= 10)).astype(float)      * 10
        sw += ((dist > 10) & (dist <= 20)).astype(float)      * 5

    # 5. ADX trend strength (10 pts)
    if _c("adx"):
        adx = df["adx"]
        sw += (adx >= 30).astype(float)                       * 10
        sw += ((adx >= 25) & (adx < 30)).astype(float)        * 7
        sw += ((adx >= 20) & (adx < 25)).astype(float)        * 3

    # Bonuses
    if _c("macd_bullish"):  sw += df["macd_bullish"].astype(float) * 5
    if _c("up_days"):       sw += (df["up_days"] >= 2).astype(float) * 3
    if _c("vcp"):           sw += df["vcp"].astype(float)       * 4
    if _c("range_bo"):      sw += df["range_bo"].astype(float)  * 3
    if _c("bb_squeeze"):    sw += df["bb_squeeze"].astype(float) * 3

    df["swing_score"] = sw.clip(0, 100).round(1)
    return df


# ── position persistence ──────────────────────────────────────────────────────
def save_positions() -> None:
    """Write all positions to JSON so they survive browser refresh."""
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    for sym, pos in st.session_state.positions.items():
        p = dict(pos)
        p["fills"] = [
            {**f, "time": f["time"].isoformat() if isinstance(f.get("time"), datetime) else f.get("time", "")}
            for f in p.get("fills", [])
        ]
        data[sym] = p
    with open(POSITIONS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_positions_from_file() -> dict:
    """Load persisted positions from JSON."""
    if not POSITIONS_FILE.exists():
        return {}
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for pos in data.values():
            for f in pos.get("fills", []):
                if isinstance(f.get("time"), str):
                    try:
                        f["time"] = datetime.fromisoformat(f["time"])
                    except Exception:
                        pass
        return data
    except Exception as e:
        _log.warning(f"Could not load positions file: {e}")
        return {}


def sync_with_kite(kite: KiteConnect) -> None:
    """
    Merge file positions with live Kite day-positions:
      - Position in Kite (qty>0) but missing here → restore with default SL/target.
      - Position here (open) but Kite shows qty=0  → mark closed.
    """
    try:
        kite_day = kite.positions().get("day", [])
    except Exception as e:
        _log.warning(f"Could not fetch Kite positions: {e}")
        return

    kite_open = {
        p["tradingsymbol"]: p
        for p in kite_day
        if int(p.get("quantity", 0)) > 0 and p.get("product") == "MIS"
    }

    # mark closed if Kite says qty = 0 — but never close paper-trade positions this way
    for sym, pos in st.session_state.positions.items():
        is_paper = pos.get("paper", False) or str(pos.get("order_id", "")).startswith("PAPER-")
        if pos["status"] == "open" and sym not in kite_open and not is_paper:
            pos["status"] = "closed"
            _log.info(f"Position {sym} closed — not found in Kite positions.")

    # restore positions found in Kite but not tracked here
    for sym, kp in kite_open.items():
        existing = st.session_state.positions.get(sym, {})
        if existing.get("status") == "open":
            # already tracked — just update qty from Kite (handles partial fills)
            existing["qty"] = int(kp["quantity"])
            continue
        avg   = float(kp.get("average_price") or 0)
        qty   = int(kp["quantity"])
        sl    = round(avg * (1 - INIT_SL_PCT / 100), 2)
        tgt   = round(avg * (1 + INIT_TGT_PCT / 100), 2)
        st.session_state.positions[sym] = {
            "symbol":       sym,
            "name":         sym,
            "qty":          qty,
            "buy_price":    avg,
            "sl":           sl,
            "target":       tgt,
            "order_id":     "restored",
            "sl_order_id":  None,
            "tgt_order_id": None,
            "status":       "open",
            "fills":        [{"action": "BUY", "qty": qty, "price": avg, "time": datetime.now()}],
        }
        _log.info(f"Restored {sym} from Kite: qty={qty} avg=₹{avg:.2f} SL=₹{sl:.2f} Tgt=₹{tgt:.2f}")
    save_positions()


def init_positions(kite: KiteConnect) -> None:
    """Run once per browser session: load file, then sync with Kite."""
    if st.session_state.get("positions_initialized"):
        return
    saved = load_positions_from_file()
    if saved:
        st.session_state.positions = saved
    sync_with_kite(kite)
    st.session_state["positions_initialized"] = True


# ── order placement ───────────────────────────────────────────────────────────
def fetch_margin_required(kite: KiteConnect, sym: str, qty: int, ltp: float) -> float:
    """Return estimated margin required for a MIS market buy order. 0 on error."""
    try:
        result = kite.order_margins([{
            "exchange":        "NSE",
            "tradingsymbol":   sym,
            "transaction_type": "BUY",
            "variety":         "regular",
            "product":         "MIS",
            "order_type":      "MARKET",
            "quantity":        qty,
            "price":           ltp,
        }])
        if result:
            return float(result[0].get("total", 0) or 0)
    except Exception:
        pass
    return 0.0


def _place_kite_order(kite: KiteConnect, sym: str, qty: int,
                      txn: str, paper: bool, price: float = 0.0,
                      exchange: str = "NSE", tradingsymbol: str | None = None,
                      product: str | None = None) -> str | None:
    """Place a limit order. For futures pass exchange='NFO', tradingsymbol=fut_ts, product=NRML."""
    if paper:
        return f"PAPER-{txn}-{sym}-{qty}"
    if price <= 0:
        price = 0.0
    limit_price  = round(price * 1.002, 2) if txn == "BUY" else round(price * 0.998, 2)
    ts           = tradingsymbol or sym
    prod         = product or (KiteConnect.PRODUCT_NRML if exchange == "NFO" else KiteConnect.PRODUCT_MIS)
    try:
        order_id = kite.place_order(
            variety=KiteConnect.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=ts,
            transaction_type=txn,
            quantity=qty,
            order_type=KiteConnect.ORDER_TYPE_LIMIT,
            price=limit_price,
            product=prod,
        )
        return order_id
    except Exception as e:
        msg = f"Order error ({exchange}:{ts} {txn} qty={qty} price={limit_price}): {e}"
        st.session_state.setdefault("order_errors", []).append(msg)
        _log.error(msg)
        return None


def do_buy(kite: KiteConnect, sym: str, name: str, qty: int, ltp: float, paper: bool,
           fut_info: dict | None = None) -> None:
    """Place a BUY order. fut_info = {tradingsymbol, lot_size, expiry, nfo_key} for futures."""
    if sym in st.session_state.positions and st.session_state.positions[sym]["status"] == "open":
        st.warning(f"Already have an open position in {sym}.")
        return

    is_fut    = bool(fut_info)
    exchange  = "NFO" if is_fut else "NSE"
    fut_ts    = fut_info["tradingsymbol"] if is_fut else sym
    product   = KiteConnect.PRODUCT_NRML if is_fut else KiteConnect.PRODUCT_MIS

    margin = fetch_margin_required(kite, sym, qty, ltp)
    sl     = round(ltp * (1 - INIT_SL_PCT / 100), 2)
    target = round(ltp * (1 + INIT_TGT_PCT / 100), 2)

    buy_order_id = _place_kite_order(kite, sym, qty, KiteConnect.TRANSACTION_TYPE_BUY,
                                     paper, ltp, exchange=exchange,
                                     tradingsymbol=fut_ts, product=product)
    if buy_order_id is None:
        return

    # place SL-M order
    sl_order_id = None
    if not paper:
        try:
            sl_order_id = kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=fut_ts,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=KiteConnect.ORDER_TYPE_SLM,
                trigger_price=sl,
                product=product,
            )
        except Exception as e:
            st.warning(f"SL order placement failed ({sym}): {e}")
    else:
        sl_order_id = f"PAPER-SL-{sym}"

    # place target limit order
    tgt_order_id = None
    if not paper:
        try:
            tgt_order_id = kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=fut_ts,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=KiteConnect.ORDER_TYPE_LIMIT,
                price=target,
                product=product,
            )
        except Exception as e:
            st.warning(f"Target order placement failed ({sym}): {e}")
    else:
        tgt_order_id = f"PAPER-TGT-{sym}"

    pos = {
        "symbol":        sym,
        "name":          name,
        "qty":           qty,
        "buy_price":     ltp,
        "sl":            sl,
        "target":        target,
        "order_id":      buy_order_id,
        "sl_order_id":   sl_order_id,
        "tgt_order_id":  tgt_order_id,
        "status":        "open",
        "paper":         paper,
        "fills":         [{"action": "BUY", "qty": qty, "price": ltp, "time": datetime.now()}],
        # futures metadata — stored so SELL/SL/target modify also use NFO exchange
        "fut_info":      fut_info,
    }
    st.session_state.positions[sym] = pos

    save_positions()
    tag = "PAPER" if paper else "LIVE"
    st.toast(f"✅ [{tag}] BUY {sym}  qty={qty}  @ ₹{ltp:.2f}  SL ₹{sl:.2f}  Tgt ₹{target:.2f}", icon="✅")

    # Telegram signal
    _vol_surge = st.session_state.get(f"_last_vol_{sym}", 1.0)
    _score     = st.session_state.get(f"_last_score_{sym}", 50.0)
    _strategy  = st.session_state.get("k_strategy", "Momentum (already moving)")
    tg_signal(sym, name, ltp, sl, target, _vol_surge, _score, _strategy, paper)

    # entry email
    mailer.email_entry("RAMA-MANIMINDSMONEY", paper, _log, {
        "Mode":       "PAPER" if paper else "LIVE",
        "Symbol":     sym,
        "Name":       name,
        "Side":       "BUY",
        "Qty":        qty,
        "Entry":      f"₹{ltp:.2f}",
        "SL":         f"₹{sl:.2f} ({INIT_SL_PCT:.1f}%)",
        "Target":     f"₹{target:.2f} ({INIT_TGT_PCT:.1f}%)",
        "Margin Req": f"₹{margin:,.2f}" if margin else "N/A",
        "Order ID":   buy_order_id,
        "Time":       datetime.now().strftime("%H:%M:%S"),
    })


def cancel_sl_target_orders(kite: KiteConnect, pos: dict, paper: bool) -> None:
    """Cancel both SL-M and target orders on Kite, then mark position as held."""
    if not paper:
        for key in ("sl_order_id", "tgt_order_id"):
            oid = pos.get(key)
            if oid:
                try:
                    kite.cancel_order(variety=KiteConnect.VARIETY_REGULAR, order_id=str(oid))
                except Exception:
                    pass
    pos["sl_order_id"]  = None
    pos["tgt_order_id"] = None
    pos["held"]         = True
    save_positions()


def restore_sl_target_orders(kite: KiteConnect, pos: dict, paper: bool) -> None:
    """Re-place SL-M and target orders after removing hold."""
    pos["held"] = False
    if not paper:
        sym = pos["symbol"]
        qty = pos["qty"]
        try:
            sl_oid = kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange="NSE", tradingsymbol=sym,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=KiteConnect.ORDER_TYPE_SLM,
                trigger_price=pos["sl"],
                product=KiteConnect.PRODUCT_MIS,
            )
            pos["sl_order_id"] = sl_oid
        except Exception as e:
            st.session_state.setdefault("order_errors", []).append(f"SL re-place failed: {e}")
        try:
            tgt_oid = kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange="NSE", tradingsymbol=sym,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=KiteConnect.ORDER_TYPE_LIMIT,
                price=pos["target"],
                product=KiteConnect.PRODUCT_MIS,
            )
            pos["tgt_order_id"] = tgt_oid
        except Exception as e:
            st.session_state.setdefault("order_errors", []).append(f"Target re-place failed: {e}")
    save_positions()


def modify_sl_order(kite: KiteConnect, pos: dict, new_sl: float, paper: bool) -> bool:
    """Move SL-M order trigger to new_sl. Returns True on success."""
    if paper:
        pos["sl"] = new_sl
        save_positions()
        return True
    oid = pos.get("sl_order_id")
    if not oid:
        pos["sl"] = new_sl
        save_positions()
        return True
    try:
        kite.modify_order(
            variety=KiteConnect.VARIETY_REGULAR,
            order_id=oid,
            trigger_price=new_sl,
        )
        pos["sl"] = new_sl
        save_positions()
        return True
    except Exception as e:
        st.warning(f"Could not modify SL order {oid}: {e}")
        return False


def modify_target_order(kite: KiteConnect, pos: dict, new_target: float, paper: bool) -> bool:
    """Move limit target order price to new_target. Returns True on success."""
    if paper:
        pos["target"] = new_target
        save_positions()
        return True
    oid = pos.get("tgt_order_id")
    if not oid:
        pos["target"] = new_target
        save_positions()
        return True
    try:
        kite.modify_order(
            variety=KiteConnect.VARIETY_REGULAR,
            order_id=oid,
            price=new_target,
        )
        pos["target"] = new_target
        save_positions()
        return True
    except Exception as e:
        st.warning(f"Could not modify target order {oid}: {e}")
        return False


def do_sell(kite: KiteConnect, sym: str, qty: int, ltp: float, paper: bool,
            reason: str = "MANUAL") -> None:
    pos = st.session_state.positions.get(sym)
    if not pos or pos["status"] != "open":
        return
    _fi       = pos.get("fut_info") or {}
    _exchange = "NFO" if _fi else "NSE"
    _fut_ts   = _fi.get("tradingsymbol") if _fi else None
    _product  = KiteConnect.PRODUCT_NRML if _fi else KiteConnect.PRODUCT_MIS
    # futures: qty must be a multiple of lot size — clamp silently
    _lot_sz = int(_fi.get("lot_size", 0)) if _fi else 0
    if _lot_sz > 0:
        qty = max(_lot_sz, (qty // _lot_sz) * _lot_sz)
        qty = min(qty, pos["qty"])   # never sell more than held
    order_id = _place_kite_order(kite, sym, qty, KiteConnect.TRANSACTION_TYPE_SELL,
                                 paper, ltp, exchange=_exchange,
                                 tradingsymbol=_fut_ts, product=_product)
    if order_id is None:
        return

    pos["qty"] -= qty
    fill = {"action": "SELL", "qty": qty, "price": ltp,
            "time": datetime.now(), "reason": reason,
            "P&L": round((ltp - pos["buy_price"]) * qty, 2)}
    pos["fills"].append(fill)
    if pos["qty"] <= 0:
        pos["status"] = "closed"

    pnl = (ltp - pos["buy_price"]) * qty
    save_positions()
    tag = "PAPER" if paper else "LIVE"
    st.toast(f"{'✅' if pnl>=0 else '❌'} [{tag}] SELL {sym}  qty={qty}  @ ₹{ltp:.2f}  P&L ₹{pnl:+,.2f}",
             icon="✅" if pnl >= 0 else "❌")

    # Telegram exit alert
    tg_exit(sym, pos.get("name", sym), pos["buy_price"], ltp, qty, reason, paper)

    # exit email
    total_pnl = sum(
        (f["price"] - pos["buy_price"]) * f["qty"]
        for f in pos["fills"] if f["action"] == "SELL"
    )
    mailer.email_trade_closed("RAMA-MANIMINDSMONEY", paper, _log,
        details={
            "Symbol":     sym,
            "Name":       pos.get("name", ""),
            "Side":       "SELL",
            "Qty sold":   qty,
            "Remaining":  pos["qty"],
            "Buy price":  f"₹{pos['buy_price']:.2f}",
            "Sell price": f"₹{ltp:.2f}",
            "Reason":     reason,
            "Time":       datetime.now().strftime("%H:%M:%S"),
        },
        fills=[{
            "Time":   f["time"].strftime("%H:%M:%S"),
            "Action": f["action"],
            "Qty":    f["qty"],
            "Price":  f"₹{f['price']:.2f}",
            "Reason": f.get("reason", ""),
            "P&L":    f.get("P&L", ""),
        } for f in pos["fills"]],
        pnl=total_pnl,
    )


# ── trade journal ─────────────────────────────────────────────────────────────
def _load_journal() -> dict:
    if JOURNAL_FILE.exists():
        try:
            with open(JOURNAL_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def _save_journal(journal: dict) -> None:
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_FILE, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, indent=2, ensure_ascii=False, default=str)


def _journal_key(sym: str, buy_price: float) -> str:
    """Unique key per trade: symbol + buy price (handles re-entries)."""
    return f"{sym}@{buy_price:.2f}"


# ── price alerts ──────────────────────────────────────────────────────────────
def _load_alerts() -> dict:
    if ALERTS_FILE.exists():
        try:
            with open(ALERTS_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def _save_alerts(alerts: dict) -> None:
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_FILE, "w", encoding="utf-8") as fh:
        json.dump(alerts, fh, indent=2, ensure_ascii=False, default=str)


def add_price_alert(sym: str, price: float, direction: str,
                    note: str = "", candle_close: bool = False) -> None:
    """direction = 'above' | 'below'. candle_close=True requires 5-min candle to close beyond price."""
    alerts = _load_alerts()
    alerts.setdefault(sym, [])
    alerts[sym].append({
        "price":        round(price, 2),
        "direction":    direction,
        "note":         note,
        "candle_close": candle_close,
        "created":      datetime.now().strftime("%Y-%m-%d %H:%M"),
        "triggered":    False,
    })
    _save_alerts(alerts)


@st.cache_data(ttl=60, show_spinner=False)
def _get_5min_close(sym: str) -> float:
    """Return the last COMPLETED 5-min candle close. Returns 0.0 on failure."""
    try:
        df = yf.Ticker(sym + ".NS").history(period="1d", interval="5m", auto_adjust=True)
        if df is None or df.empty or len(df) < 2:
            return 0.0
        # iloc[-1] is the in-progress candle — use iloc[-2] (last completed)
        return float(df["Close"].iloc[-2])
    except Exception:
        return 0.0


def check_price_alerts(quotes: dict[str, dict]) -> None:
    """Run every refresh — fires Telegram for triggered alerts then marks them done."""
    alerts = _load_alerts()
    changed = False
    for sym, sym_alerts in alerts.items():
        ltp = quotes.get(sym, {}).get("last_price", 0.0)
        if not ltp:
            continue
        for a in sym_alerts:
            if a.get("triggered"):
                continue
            price     = a["price"]
            direction = a["direction"]
            if a.get("candle_close"):
                # 5-min candle close confirmation — ignore LTP wicks
                close5 = _get_5min_close(sym)
                if close5 <= 0:
                    continue
                hit = (direction == "above" and close5 >= price) or \
                      (direction == "below" and close5 <= price)
                trigger_price = close5
                confirm_label = f"5m candle closed {'above' if direction == 'above' else 'below'}"
            else:
                hit = (direction == "above" and ltp >= price) or \
                      (direction == "below" and ltp <= price)
                trigger_price = ltp
                confirm_label = f"LTP {'crossed above' if direction == 'above' else 'crossed below'}"

            if hit:
                a["triggered"]     = True
                a["trigger_price"] = round(trigger_price, 2)
                a["trigger_time"]  = datetime.now().strftime("%Y-%m-%d %H:%M")
                changed = True
                _dir_icon  = "🚀" if direction == "above" else "⚠️"
                _note_line = f"\n📌 Note: {a['note']}" if a.get("note") else ""
                _conf_icon = "🕯 5-min candle close" if a.get("candle_close") else "⚡ LTP tick"
                send_alert(
                    f"🏦 *MANIMINDSMONEY | RAMA*\n"
                    f"{'─'*28}\n"
                    f"{_dir_icon} *PRICE ALERT — {sym}*\n"
                    f"{'─'*28}\n"
                    f"📍 Trigger: ₹{trigger_price:,.2f}\n"
                    f"🎯 Alert:   ₹{price:,.2f} ({confirm_label})\n"
                    f"📊 LTP now: ₹{ltp:,.2f}\n"
                    f"{_conf_icon}\n"
                    f"{_note_line}\n"
                    f"🕐 {datetime.now():%d %b %Y %H:%M}"
                )
    if changed:
        _save_alerts(alerts)


# ── positions panel ───────────────────────────────────────────────────────────
def render_positions_panel(kite: KiteConnect, quotes: dict[str, dict], paper: bool) -> None:
    active = {s: p for s, p in st.session_state.positions.items() if p["status"] == "open"}
    if not active:
        return

    st.markdown("### 📊 Active Positions")
    for sym, pos in list(active.items()):
        ltp      = quotes.get(sym, {}).get("last_price", pos["buy_price"])
        pnl      = (ltp - pos["buy_price"]) * pos["qty"]
        pnl_pct  = (ltp - pos["buy_price"]) / pos["buy_price"] * 100
        sl_pct   = (pos["sl"]     - pos["buy_price"]) / pos["buy_price"] * 100
        tgt_pct  = (pos["target"] - pos["buy_price"]) / pos["buy_price"] * 100
        is_profit  = pnl >= 0
        is_held    = pos.get("held", False)
        border_col = "#ff9800" if is_held else ("#4caf50" if is_profit else "#f44336")
        bg_col     = "#fff8e1" if is_held else ("#f1f8f1" if is_profit else "#fff1f1")
        pnl_col    = "#1b5e20" if is_profit else "#b71c1c"
        sl_style   = "color:#aaa;text-decoration:line-through" if is_held else ""
        tgt_style  = "color:#aaa;text-decoration:line-through" if is_held else ""

        # auto SL / target check — skipped when position is held
        sl_hit  = (not is_held) and ltp <= pos["sl"]
        tgt_hit = (not is_held) and ltp >= pos["target"]

        held_badge = ("&nbsp;<span style='background:#ff9800;color:white;border-radius:4px;"
                      "padding:1px 7px;font-size:12px;font-weight:700'>HELD</span>") if is_held else ""

        st.markdown(
            f"""
            <div style='border:2px solid {border_col};border-radius:10px;
                        padding:12px 18px;margin:4px 0;background:{bg_col}'>
              <span style='font-size:16px;font-weight:700'>{sym}</span>
              &nbsp;·&nbsp; {pos.get("name","")[:30]}
              {held_badge}
              &nbsp;&nbsp;
              Qty: <b>{pos["qty"]}</b>
              &nbsp;|&nbsp; Buy: <b>₹{pos["buy_price"]:.2f}</b>
              &nbsp;|&nbsp; LTP: <b>₹{ltp:.2f}</b>
              &nbsp;|&nbsp;
              <span style='color:{pnl_col};font-size:16px;font-weight:700'>
                P&amp;L: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)
              </span>
              <br>
              <span style='{sl_style}'>SL: <b>₹{pos["sl"]:.2f}</b> ({sl_pct:.2f}%)</span>
              &nbsp;
              <span style='font-size:11px;color:#888'>
                {"[SL order: " + str(pos.get("sl_order_id","—")) + "]"}
              </span>
              &nbsp;&nbsp;
              <span style='{tgt_style}'>Target: <b>₹{pos["target"]:.2f}</b> ({tgt_pct:.2f}%)</span>
              &nbsp;
              <span style='font-size:11px;color:#888'>
                {"[Tgt order: " + str(pos.get("tgt_order_id","—")) + "]"}
              </span>
              {"&nbsp; <span style='color:red;font-weight:bold'>⚠️ SL HIT</span>" if sl_hit else ""}
              {"&nbsp; <span style='color:green;font-weight:bold'>🎯 TARGET HIT</span>" if tgt_hit else ""}
            </div>
            """,
            unsafe_allow_html=True,
        )

        c1, c2, c3, c4, c5, c6 = st.columns([1.1, 1.1, 1.3, 1.3, 1.1, 1.5])

        _fi_pos  = pos.get("fut_info") or {}
        _lot_sz  = int(_fi_pos.get("lot_size", 0)) if _fi_pos else 0
        _is_fut_pos = _lot_sz > 0

        with c1:
            if _is_fut_pos:
                # futures: sell in whole lots only
                _total_lots = pos["qty"] // _lot_sz
                _lot_opts   = {f"{n} lot{'s' if n>1 else ''}": n * _lot_sz
                               for n in range(1, _total_lots + 1)}
                _lot_opts   = _lot_opts or {f"1 lot": _lot_sz}
                sel = st.selectbox("Lots to sell", list(_lot_opts.keys()),
                                   key=f"pct_{sym}", label_visibility="collapsed")
                sell_qty = _lot_opts[sel]
            else:
                pct_opts = {"100%": 1.0, "75%": 0.75, "50%": 0.50, "25%": 0.25}
                sel = st.selectbox("Partial sell", list(pct_opts.keys()),
                                   key=f"pct_{sym}", label_visibility="collapsed")
                sell_qty = max(1, int(pos["qty"] * pct_opts[sel]))

        with c2:
            if st.button(f"SELL {sell_qty}", key=f"sell_pos_{sym}", type="primary"):
                do_sell(kite, sym, sell_qty, ltp, paper)
                st.rerun()

        with c3:
            sl_pct_now = round((pos["sl"] - pos["buy_price"]) / pos["buy_price"] * 100, 4)
            if st.button(f"Trail SL ↑0.25%  ({sl_pct_now:+.2f}%)", key=f"trail_sl_{sym}",
                         disabled=is_held, help="Remove Hold first to trail SL"):
                step       = round(pos["buy_price"] * TRAIL_STEP_PCT / 100, 2)
                new_sl     = round(pos["sl"] + step, 2)
                new_sl_pct = round((new_sl - pos["buy_price"]) / pos["buy_price"] * 100, 2)
                ok = modify_sl_order(kite, pos, new_sl, paper)
                if ok:
                    st.toast(f"SL → ₹{new_sl:.2f} ({new_sl_pct:+.2f}% from buy)", icon="🔒")
                st.rerun()

        with c4:
            tgt_pct_now = round((pos["target"] - pos["buy_price"]) / pos["buy_price"] * 100, 4)
            if st.button(f"Trail Tgt ↑0.25%  ({tgt_pct_now:+.2f}%)", key=f"trail_tgt_{sym}",
                         disabled=is_held, help="Remove Hold first to trail target"):
                step        = round(pos["buy_price"] * TRAIL_STEP_PCT / 100, 2)
                new_tgt     = round(pos["target"] + step, 2)
                new_tgt_pct = round((new_tgt - pos["buy_price"]) / pos["buy_price"] * 100, 2)
                ok = modify_target_order(kite, pos, new_tgt, paper)
                if ok:
                    st.toast(f"Target → ₹{new_tgt:.2f} ({new_tgt_pct:+.2f}% from buy)", icon="🎯")
                st.rerun()

        with c5:
            if is_held:
                if st.button("▶ Remove Hold", key=f"unhold_{sym}",
                             help="Re-place SL and target orders and resume monitoring"):
                    restore_sl_target_orders(kite, pos, paper)
                    st.toast(f"{sym} — SL & target orders restored", icon="▶")
                    st.rerun()
            else:
                if st.button("⏸ Hold", key=f"hold_{sym}",
                             help="Cancel SL & target orders and hold freely"):
                    cancel_sl_target_orders(kite, pos, paper)
                    st.toast(f"{sym} — SL & target cancelled, position held", icon="⏸")
                    st.rerun()

        with c6:
            if sl_hit:
                st.error(f"Auto-selling (SL hit) {sym}")
                do_sell(kite, sym, pos["qty"], ltp, paper, reason="SL HIT")
                st.rerun()
            elif tgt_hit:
                st.warning(f"🎯 Target hit — click SELL to book.")

        # ── journal popover — full width below the button row ─────────────────
        _jkey   = _journal_key(sym, pos["buy_price"])
        _jstore = _load_journal()
        _jentry = _jstore.get(_jkey, {"note": "", "updated": ""})
        _has_note = bool(_jentry.get("note", "").strip())
        _btn_label = "📝 Journal ✓" if _has_note else "📝 Journal"

        with st.popover(_btn_label, use_container_width=False):
            st.markdown(f"**Trade Journal — {sym}**")
            st.caption(f"Buy ₹{pos['buy_price']:.2f} · Qty {pos['qty']}"
                       + (f" · Last saved: {_jentry['updated']}" if _jentry.get("updated") else ""))
            _note_key = f"journal_note_{_jkey}"
            if _note_key not in st.session_state:
                st.session_state[_note_key] = _jentry.get("note", "")

            def _save_note(_k=_jkey, _nk=_note_key):
                _text = st.session_state.get(_nk, "").strip()
                _j    = _load_journal()
                _j[_k] = {"note": _text, "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
                _save_journal(_j)
                st.session_state["_journal_saved"] = _k

            st.text_area(
                "Notes", key=_note_key, height=140,
                placeholder="Why did you enter? Plan, emotions, what went wrong/right…",
                label_visibility="collapsed",
            )
            st.button("💾 Save note", key=f"jrnl_save_{_jkey}", on_click=_save_note,
                      use_container_width=True, type="primary")
            if st.session_state.pop("_journal_saved", None) == _jkey:
                st.success("Saved ✓")

        st.markdown("---")


def render_screener_table(final: pd.DataFrame, kite: KiteConnect,
                          quotes: dict[str, dict], paper: bool,
                          news_data: dict | None = None,
                          lot_sizes: dict[str, int] | None = None) -> None:
    if final.empty:
        st.info("No stocks match current filters. Try disabling technical/fundamental toggles.")
        return

    # ── search bar ───────────────────────────────────────────────────────────
    # Must clear BEFORE the widget is instantiated — Streamlit forbids modifying
    # a keyed widget's state after it has been created in the same run.
    if st.session_state.pop("_clear_search", False):
        st.session_state["tbl_search"] = ""

    _sc, _xc = st.columns([10, 1])
    with _sc:
        st.text_input("🔍 Search stock name or symbol",
                      placeholder="e.g. Reliance, TCS, INFY — press Enter to filter",
                      key="tbl_search")
    with _xc:
        st.markdown("<div style='margin-top:28px'>", unsafe_allow_html=True)
        if st.button("✕ Clear", key="clear_search_btn", use_container_width=True):
            st.session_state["_clear_search"] = True
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    search = st.session_state.get("tbl_search", "").strip().lower()
    if search:
        mask  = (final["symbol"].str.lower().str.contains(search, na=False) |
                 final["name"].str.lower().str.contains(search, na=False))
        final = final[mask]
        if final.empty:
            st.info(f"No results for '{search}'.")
            return

    # ── column definitions + sort state ──────────────────────────────────────
    HEADERS = [
        ("#",             None,           0.3),
        ("Stock\nName",   None,           2.0),
        ("Symbol",        None,           1.1),
        ("LTP\n₹",        "ltp",          0.9),
        ("Chg\n%",        "pct_change",   0.9),
        ("Vol\nSurge",    "vol_surge",    1.1),
        ("VWAP\n15m ₹",   "vwap",         0.9),
        ("RSI\n14",       "rsi",          0.7),
        ("UC\nDist%",     "dist_to_uc",   0.9),
        ("52W\nHigh",     "high_52w",     0.9),
        ("52W\nLow",      "low_52w",      0.9),
        ("ADX\n14",       "adx",          0.7),
        ("Buy\nPressure", "bs_ratio",     0.7),
        ("Setup\nSignals","setup_score",  1.1),
        ("Score\n/100",   "score",        0.7),
        ("Swing\nScore",  "swing_score",  0.8),
        ("Qty",           None,           0.8),
        ("Action",        None,           1.5),
    ]
    sortable_cols = {h[1] for h in HEADERS if h[1]}

    _default_sort = "swing_score" if st.session_state.get("k_strategy") == "Swing Trade (3-5 day)" else "pct_change"
    if ("tbl_sort_col" not in st.session_state or
            st.session_state["tbl_sort_col"] not in sortable_cols or
            st.session_state["tbl_sort_col"] not in final.columns):
        st.session_state["tbl_sort_col"] = _default_sort
        st.session_state["tbl_sort_asc"] = False

    # inject CSS — active sort highlight + compact table to prevent horizontal scroll
    active_col = st.session_state["tbl_sort_col"]
    st.markdown(f"""
    <style>
    div[data-testid="column"] button[kind="secondary"][aria-label="sort_hdr_{active_col}"],
    div[data-testid="stButton"] > button#sort_hdr_{active_col} {{
        background-color: #1a73e8 !important;
        color: white !important;
        border-color: #1a73e8 !important;
    }}
    /* ── Compact table: smaller gaps between columns ─────────── */
    div[data-testid="stHorizontalBlock"] {{
        gap: 2px !important;
    }}
    div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {{
        padding: 0 1px !important;
        min-width: 0 !important;
    }}
    /* Compact cell text */
    div[data-testid="stHorizontalBlock"] small,
    div[data-testid="stHorizontalBlock"] p {{
        font-size: 11px !important;
        line-height: 1.3 !important;
        margin: 0 !important;
    }}
    /* Compact number input */
    div[data-testid="stHorizontalBlock"] div[data-testid="stNumberInput"] input {{
        font-size: 11px !important;
        padding: 2px 4px !important;
        height: 32px !important;
    }}
    /* Header sort buttons — wrap text, compact */
    div[data-testid="stButton"] > button {{
        white-space: pre-wrap !important;
        word-break: break-word !important;
        line-height: 1.2 !important;
        font-size: 10px !important;
        padding: 3px 2px !important;
        height: auto !important;
        min-height: 38px !important;
    }}
    /* Action buttons — compact */
    div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] > button {{
        font-size: 10px !important;
        padding: 3px 2px !important;
        min-height: 30px !important;
    }}
    /* ── Mobile: stack table columns vertically (card per stock) ─ */
    @media (max-width: 768px) {{
        /* Hide desktop header row (identified by its non-sortable header cells) */
        div[data-testid="stHorizontalBlock"]:has(.tbl-hdr-cell) {{
            display: none !important;
        }}
        /* Mobile inline labels: visible only on mobile */
        .mh {{
            display: inline !important;
            color: #888 !important;
            font-size: 11px !important;
            font-weight: 600 !important;
            margin-right: 4px !important;
        }}
        /* Target only 18-col table rows (after the .tbl-sep hr), stack them vertically */
        div[data-testid="stMarkdownContainer"]:has(.tbl-sep) ~ div[data-testid="stHorizontalBlock"]:has(> div:nth-child(16)) {{
            flex-direction: column !important;
            flex-wrap: nowrap !important;
            overflow-x: hidden !important;
            border: 1px solid #d0d0d0 !important;
            border-radius: 12px !important;
            padding: 14px 16px !important;
            margin-bottom: 12px !important;
            background: #fafafa !important;
            box-shadow: 0 1px 4px rgba(0,0,0,0.06) !important;
        }}
        div[data-testid="stMarkdownContainer"]:has(.tbl-sep) ~ div[data-testid="stHorizontalBlock"]:has(> div:nth-child(16)) > div[data-testid="column"] {{
            width: 100% !important;
            min-width: 100% !important;
            max-width: 100% !important;
            flex: none !important;
            padding: 3px 0 !important;
            margin-bottom: 0 !important;
            border-bottom: 1px solid #f0f0f0 !important;
        }}
        div[data-testid="stMarkdownContainer"]:has(.tbl-sep) ~ div[data-testid="stHorizontalBlock"]:has(> div:nth-child(16)) > div[data-testid="column"]:last-child {{
            border-bottom: none !important;
            margin-bottom: 0 !important;
        }}
        /* Cancel Streamlit's default -1rem bottom margin on all children inside mobile cards */
        div[data-testid="stMarkdownContainer"]:has(.tbl-sep) ~ div[data-testid="stHorizontalBlock"]:has(> div:nth-child(16)) * {{
            margin-bottom: 0 !important;
        }}
        /* Text inside mobile cards */
        div[data-testid="stMarkdownContainer"]:has(.tbl-sep) ~ div[data-testid="stHorizontalBlock"] small,
        div[data-testid="stMarkdownContainer"]:has(.tbl-sep) ~ div[data-testid="stHorizontalBlock"] p {{
            font-size: 14px !important;
            line-height: 1.6 !important;
            margin: 0 !important;
        }}
        /* Action + share buttons inside mobile cards */
        div[data-testid="stMarkdownContainer"]:has(.tbl-sep) ~ div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] > button {{
            font-size: 13px !important;
            padding: 6px 10px !important;
            min-height: 38px !important;
        }}
        div[data-testid="stMarkdownContainer"]:has(.tbl-sep) ~ div[data-testid="stHorizontalBlock"] div[data-testid="stNumberInput"] input {{
            font-size: 14px !important;
            padding: 6px 8px !important;
            height: 38px !important;
        }}
        section[data-testid="stSidebar"] {{
            min-width: 200px !important;
            max-width: 240px !important;
        }}
    }}
    </style>""", unsafe_allow_html=True)

    widths = [w for _, _, w in HEADERS]
    hcols  = st.columns(widths)

    for col, (label, df_col, _) in zip(hcols, HEADERS):
        # convert \n to <br> for markdown cells, keep \n for button cells (pre-wrap renders it)
        if df_col is None:
            html_label = label.replace("\n", "<br>")
            col.markdown(
                f"<div class='tbl-hdr-cell' style='font-size:11px;font-weight:700;line-height:1.4;"
                f"padding:4px 0;min-height:44px;display:flex;align-items:center'>"
                f"{html_label}</div>",
                unsafe_allow_html=True)
        else:
            active = active_col == df_col
            arrow  = (" ▲" if st.session_state["tbl_sort_asc"] else " ▼") if active else " ⇅"
            prefix = "● " if active else ""
            if col.button(f"{prefix}{label}{arrow}", key=f"sort_hdr_{df_col}",
                          use_container_width=True):
                if active:
                    st.session_state["tbl_sort_asc"] = not st.session_state["tbl_sort_asc"]
                else:
                    st.session_state["tbl_sort_col"] = df_col
                    st.session_state["tbl_sort_asc"] = False
                # st.rerun() needed so active_col is re-read with the new value
                # on the very next render (otherwise highlight shows one run late).
                # Safe now: search text lives in key-based session_state, not value="".
                st.rerun()

    st.markdown("<hr class='tbl-sep' style='margin:4px 0'>", unsafe_allow_html=True)

    final = final.sort_values(
        st.session_state["tbl_sort_col"],
        ascending=st.session_state["tbl_sort_asc"],
    ).reset_index(drop=True)

    # ── pagination ────────────────────────────────────────────────────────────
    _PAGE_SIZE = 50
    _total     = len(final)
    _n_pages   = max(1, math.ceil(_total / _PAGE_SIZE))
    _pg_col1, _pg_col2, _pg_col3 = st.columns([4, 2, 4])
    with _pg_col2:
        _page = st.number_input(
            "Page", min_value=1, max_value=_n_pages, value=1, step=1,
            key="tbl_page", label_visibility="collapsed",
        )
    _pg_col1.markdown(
        f"<small style='color:#888'>Showing <b>{min(_PAGE_SIZE, _total - (_page-1)*_PAGE_SIZE)}</b>"
        f" of <b>{_total}</b> stocks</small>",
        unsafe_allow_html=True,
    )
    _pg_col3.markdown(
        f"<small style='color:#888;float:right'>Page <b>{_page}</b> / <b>{_n_pages}</b></small>",
        unsafe_allow_html=True,
    )
    _start = (_page - 1) * _PAGE_SIZE
    final  = final.iloc[_start : _start + _PAGE_SIZE].reset_index(drop=True)

    # ── full-width margin banner (shown after BUY click) ──────────────────────
    pm = st.session_state.get("pending_margin", {})
    if pm:
        margin_val = pm["margin"]
        trade_val  = pm["ltp"] * pm["qty"]
        ok_margin  = margin_val > 0
        st.markdown(
            f"""
            <div style='background:#e3f2fd;border-left:5px solid #1a73e8;
                        border-radius:6px;padding:14px 24px;margin:6px 0 12px 0;
                        font-size:15px;line-height:2.2;width:100%'>
              💰 &nbsp;<b>{pm["sym"]}</b> &nbsp;·&nbsp;
              Qty:&nbsp;<b>{pm["qty"]}</b> &nbsp;·&nbsp;
              LTP:&nbsp;<b>₹{pm["ltp"]:.2f}</b> &nbsp;·&nbsp;
              Trade value:&nbsp;<b>₹{trade_val:,.2f}</b>
              &nbsp;&nbsp;&nbsp;
              <span style='color:#1a73e8;font-size:20px;font-weight:800'>
                {"Margin required: ₹" + f"{margin_val:,.2f}" if ok_margin
                 else "⚠️ Margin unavailable — market may be closed"}
              </span>
              {"&nbsp;&nbsp;<span style='color:#888;font-size:12px'>(approx MIS intraday margin)</span>" if ok_margin else ""}
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Pre-seed qty session_state to lot sizes before widgets render.
    # Streamlit ignores value= if the key already exists in session_state,
    # so we must write directly when switching to futures mode.
    if lot_sizes:
        for _sym, _lot in lot_sizes.items():
            _k = f"qty_{_sym}"
            if st.session_state.get(_k, 0) != _lot:
                st.session_state[_k] = _lot
    else:
        # switched away from futures — reset any lot-size keys back to 100
        _prev_lots = st.session_state.get("_prev_lot_sizes", {})
        for _sym, _lot in _prev_lots.items():
            _k = f"qty_{_sym}"
            if st.session_state.get(_k) == _lot:
                st.session_state[_k] = 100
    st.session_state["_prev_lot_sizes"] = dict(lot_sizes) if lot_sizes else {}

    for idx, row in final.iterrows():
        sym  = row["symbol"]
        name = row.get("name", sym)
        ltp  = row["ltp"]
        has_pos = (sym in st.session_state.positions and
                   st.session_state.positions[sym]["status"] == "open")

        pct_color  = "#1b5e20" if row["pct_change"] >= 0 else "#b71c1c"
        vwap_val   = row.get("vwap", ltp)
        above_vwap = row.get("above_vwap", True)
        vwap_color = "#1b5e20" if above_vwap else "#b71c1c"
        vwap_tag   = "▲" if above_vwap else "▼"
        rsi_val    = row.get("rsi", 50.0)
        rsi_color  = "#1b5e20" if rsi_val < 40 else ("#b71c1c" if rsi_val > 60 else "#555")

        rcols = st.columns([0.3, 2.0, 1.1, 0.9, 0.9, 1.1, 0.9, 0.7, 0.9, 0.9, 0.9, 0.7, 0.7, 1.1, 0.7, 0.8, 0.8, 1.5])

        rcols[0].markdown(f"<small>{idx+1}</small>", unsafe_allow_html=True)
        # News popover — click name to see latest headlines
        _nd        = (news_data or {}).get(sym, {})
        _headlines = _nd.get("headlines", [])
        _urls      = _nd.get("urls", [])
        _sentiment = _nd.get("sentiment", 0.0)
        _n_art     = _nd.get("n_articles", 0)
        _sources   = _nd.get("sources", [])
        _sent_icon = "🟢" if _sentiment > 0.05 else ("🔴" if _sentiment < -0.05 else "⚪")
        with rcols[1].popover(f"{name[:18]}…" if len(name) > 18 else name,
                              use_container_width=True):
            st.markdown(f"**{name}** · `{sym}`")
            if _headlines:
                _src_label = " · ".join(_sources) if _sources else "news"
                st.caption(f"{_sent_icon} Sentiment: {_sentiment:+.2f} · {_n_art} articles · {_src_label}")
                for hl, url in zip(_headlines, _urls + [""] * len(_headlines)):
                    if url:
                        st.markdown(
                            f"• <a href='{url}' target='_blank' rel='noopener noreferrer'>{hl}</a>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(f"• {hl}")
                _quick_links = (
                    f"<a href='https://www.tradingview.com/symbols/NSE-{sym}/' "
                    f"target='_blank' style='font-size:11px;margin-right:8px'>📈 TradingView</a>"
                    f"<a href='https://www.screener.in/company/{sym}/' "
                    f"target='_blank' style='font-size:11px;margin-right:8px'>📊 Screener</a>"
                    f"<a href='https://www.moneycontrol.com/india/stockpricequote/search?keyword={sym}' "
                    f"target='_blank' style='font-size:11px;margin-right:8px'>💹 Moneycontrol</a>"
                    f"<a href='https://news.google.com/search?q={sym}+NSE+stock&hl=en-IN' "
                    f"target='_blank' style='font-size:11px'>🔍 News</a>"
                )
                st.markdown(_quick_links, unsafe_allow_html=True)
            else:
                st.caption("No headlines found — open directly:")
                _quick_links = (
                    f"<a href='https://www.tradingview.com/symbols/NSE-{sym}/' "
                    f"target='_blank' style='font-size:12px;margin-right:8px'>📈 TradingView</a>"
                    f"<a href='https://www.screener.in/company/{sym}/' "
                    f"target='_blank' style='font-size:12px;margin-right:8px'>📊 Screener</a>"
                    f"<a href='https://www.moneycontrol.com/india/stockpricequote/search?keyword={sym}' "
                    f"target='_blank' style='font-size:12px;margin-right:8px'>💹 Moneycontrol</a>"
                    f"<a href='https://news.google.com/search?q={sym}+NSE+stock&hl=en-IN' "
                    f"target='_blank' style='font-size:12px'>🔍 News</a>"
                )
                st.markdown(_quick_links, unsafe_allow_html=True)
        rcols[2].markdown(f"<small><b>{sym}</b></small>", unsafe_allow_html=True)
        rcols[3].markdown(f"<small><span class='mh'>LTP</span>₹{ltp:.2f}</small>", unsafe_allow_html=True)
        rcols[4].markdown(
            f"<small><span class='mh'>Chg</span><span style='color:{pct_color};font-weight:bold'>{row['pct_change']:+.2f}%</span></small>",
            unsafe_allow_html=True)
        rcols[5].markdown(f"<small><span class='mh'>Vol</span>{row['vol_surge']:.1f}×</small>", unsafe_allow_html=True)
        rcols[6].markdown(
            f"<small><span class='mh'>VWAP</span><span style='color:{vwap_color}'>{vwap_tag}₹{vwap_val:.1f}</span></small>",
            unsafe_allow_html=True)
        rcols[7].markdown(
            f"<small><span class='mh'>RSI</span><span style='color:{rsi_color}'><b>{rsi_val:.0f}</b></span></small>",
            unsafe_allow_html=True)
        _uc_dist  = row.get("dist_to_uc", 0.0)
        _uc_price = row.get("upper_circuit", 0.0)
        _lc_price = row.get("lower_circuit", 0.0)
        _uc_color = "#b71c1c" if _uc_dist <= 1.0 else ("#e65100" if _uc_dist <= 3.0 else "#555")
        rcols[8].markdown(
            f"<small><span class='mh'>UC</span><span style='color:{_uc_color}' title='UC ₹{_uc_price:,.2f} | LC ₹{_lc_price:,.2f}'>"
            f"{_uc_dist:.1f}%</span></small>",
            unsafe_allow_html=True)
        # 52W High — green if LTP is within 5% of high
        _h52 = row.get("high_52w", 0.0)
        _h52_color = "#1b5e20" if _h52 > 0 and ((_h52 - ltp) / _h52 * 100) <= 5 else "#555"
        rcols[9].markdown(
            f"<small><span class='mh'>52H</span><span style='color:{_h52_color}'>₹{_h52:,.0f}</span></small>",
            unsafe_allow_html=True)
        # 52W Low — red if LTP is within 10% of low
        _l52 = row.get("low_52w", 0.0)
        _l52_color = "#b71c1c" if _l52 > 0 and ((ltp - _l52) / _l52 * 100) <= 10 else "#555"
        rcols[10].markdown(
            f"<small><span class='mh'>52L</span><span style='color:{_l52_color}'>₹{_l52:,.0f}</span></small>",
            unsafe_allow_html=True)

        # ADX — green ≥25 (trending), orange 20-25 (developing), grey <20 (choppy)
        _adx = row.get("adx", 20.0)
        _adx_color = ("#1b5e20" if _adx >= 25 else ("#e65100" if _adx >= 20 else "#9e9e9e"))
        _adx_tip   = "●" if _adx >= 25 else ("○" if _adx >= 20 else "–")
        rcols[11].markdown(
            f"<small><span class='mh'>ADX</span><span style='color:{_adx_color};font-weight:bold'>{_adx_tip}{_adx:.0f}</span></small>",
            unsafe_allow_html=True)

        # Buy Pressure — ratio of buy-candle days + today's direction
        _bs = row.get("bs_ratio", 0.5)
        _bs_pct = round(_bs * 100)
        _bs_color = ("#1b5e20" if _bs >= 0.65 else ("#e65100" if _bs >= 0.5 else "#b71c1c"))
        _bs_icon  = "🟢" if _bs >= 0.65 else ("🟡" if _bs >= 0.5 else "🔴")
        rcols[12].markdown(
            f"<small><span class='mh'>BP</span><span style='color:{_bs_color};font-weight:bold'>{_bs_icon}{_bs_pct}%</span></small>",
            unsafe_allow_html=True)

        # Setup badges
        setup_pts = int(row.get("setup_score", 0))
        badges = []
        if row.get("vcp"):           badges.append("🔴VCP")
        if row.get("range_bo"):      badges.append("🟠RBO")
        if row.get("inside_bar"):    badges.append("🟤IB")
        if row.get("bb_squeeze"):    badges.append("🔵BB")
        if row.get("nr7"):           badges.append("🟣NR7")
        if row.get("near_day_high"): badges.append("🟢H")
        if row.get("intraday_range", 99) <= 1.5:   badges.append("⚪Tight")
        if 45 <= row.get("rsi", 0) <= 60:          badges.append("🟡RSI")
        # EMA alignment badges
        _e21, _e50, _e200 = row.get("above_ema21"), row.get("above_ema50"), row.get("above_ema200")
        if _e21 and _e50 and _e200:  badges.append("🟢E3")
        elif _e21 and _e50:          badges.append("🟡E2")
        elif _e21:                   badges.append("⚪E1")
        _ns = row.get("news_sentiment", 0.0)
        if row.get("news_has_results"):             badges.append("📋Res")
        if _ns > 0.1:                              badges.append(f"📰+{_ns:.2f}")
        elif _ns < -0.1:                           badges.append(f"📰{_ns:.2f}")
        badge_str = " ".join(badges) if badges else "—"
        rcols[13].markdown(
            f"<small><span class='mh'>Setup</span><span style='font-size:10px'>{badge_str}</span></small>",
            unsafe_allow_html=True)

        rcols[14].markdown(f"<small><span class='mh'>Score</span><b>{row['score']:.0f}</b></small>", unsafe_allow_html=True)

        # Swing score — colour-coded by quality
        _sw = row.get("swing_score", 0.0)
        _sw_color = "#1b5e20" if _sw >= 70 else ("#e65100" if _sw >= 50 else "#555")
        rcols[15].markdown(
            f"<small><span class='mh'>Swing</span><span style='color:{_sw_color};font-weight:bold'>{_sw:.0f}</span></small>",
            unsafe_allow_html=True)

        with rcols[16]:
            _lot  = (lot_sizes or {}).get(sym, 0)
            _step = _lot if _lot > 0 else 1
            _def_qty = _lot if _lot > 0 else 100
            qty = st.number_input("qty", min_value=1, value=_def_qty, step=_step,
                                  key=f"qty_{sym}", label_visibility="collapsed",
                                  help=f"Lot size: {_lot}" if _lot > 0 else "Qty")

        with rcols[17]:
            # ── Row 1: trade actions ──────────────────────────────────────
            bc, sc, alc = st.columns(3)
            with bc:
                _fi = (lot_sizes and sym in lot_sizes and
                       st.session_state.get("_fut_instruments", {}).get(sym))
                _buy_label = "BUY FUT" if _fi else "BUY"
                if st.button(_buy_label, key=f"buy_{sym}",
                             disabled=has_pos, type="primary",
                             use_container_width=True):
                    real_ltp = quotes.get(sym, {}).get("last_price", ltp)
                    margin   = fetch_margin_required(kite, sym, qty, real_ltp)
                    st.session_state["pending_margin"] = {"sym": sym, "margin": margin, "ltp": real_ltp, "qty": qty}
                    st.session_state[f"_last_vol_{sym}"]   = row.get("vol_surge", 1.0)
                    st.session_state[f"_last_score_{sym}"] = row.get("score", 50.0)
                    _fut_info = st.session_state.get("_fut_instruments", {}).get(sym)
                    do_buy(kite, sym, name, qty, real_ltp, paper, fut_info=_fut_info)
                    st.rerun()
            with sc:
                if st.button("SELL", key=f"sell_tbl_{sym}",
                             disabled=not has_pos,
                             use_container_width=True):
                    pos      = st.session_state.positions[sym]
                    real_ltp = quotes.get(sym, {}).get("last_price", ltp)
                    do_sell(kite, sym, pos["qty"], real_ltp, paper)
                    st.rerun()

            # ── alert popover (row 1, col 3) ──────────────────────────────
            with alc:
                _existing_alerts = [a for a in _load_alerts().get(sym, [])
                                    if not a.get("triggered")]
                _al_label = "🔔✓" if _existing_alerts else "🔔"
                with st.popover(_al_label, help="Set price alert → Telegram"):
                    st.markdown(f"**Price Alert — {sym}**")
                    st.caption(f"LTP: ₹{ltp:,.2f}")
                    _ap_key  = f"alert_price_{sym}"
                    _ad_key  = f"alert_dir_{sym}"
                    _an_key  = f"alert_note_{sym}"
                    _alert_price = st.number_input(
                        "Alert price ₹", min_value=0.01,
                        value=float(round(ltp, 2)), step=0.05,
                        key=_ap_key, format="%.2f",
                    )
                    _alert_dir = st.radio(
                        "Trigger when price",
                        ["crosses above ↑", "crosses below ↓"],
                        key=_ad_key, horizontal=True,
                    )
                    _alert_note = st.text_input(
                        "Note (optional)", key=_an_key,
                        placeholder="e.g. breakout level",
                    )
                    _acc_key = f"alert_candle_{sym}"
                    _candle_close = st.toggle(
                        "🕯 Confirm on 5-min candle close",
                        key=_acc_key, value=False,
                        help="OFF = alert fires as soon as LTP crosses the price (may be a wick).\n"
                             "ON = alert fires only when a 5-min candle CLOSES above/below the price (more reliable).",
                    )
                    if _candle_close:
                        st.caption("⚡ Will wait for a 5-min candle to close beyond ₹{:.2f}".format(
                            st.session_state.get(_ap_key, ltp)))
                    if st.button("Set Alert 🔔", key=f"alert_set_{sym}",
                                 use_container_width=True, type="primary",
                                 disabled=not _tg_enabled()):
                        _direction = "above" if "above" in _alert_dir else "below"
                        add_price_alert(sym, _alert_price, _direction, _alert_note, _candle_close)
                        _mode = "5m candle close" if _candle_close else "LTP tick"
                        st.toast(f"🔔 Alert set: {sym} {'≥' if _direction=='above' else '≤'} ₹{_alert_price:,.2f} · {_mode}")
                    # show active alerts for this stock
                    if _existing_alerts:
                        st.markdown("**Active alerts:**")
                        for _i, _a in enumerate(_existing_alerts):
                            _dir_sym  = "≥" if _a["direction"] == "above" else "≤"
                            _cc_badge = " 🕯5m" if _a.get("candle_close") else " ⚡LTP"
                            ac1, ac2 = st.columns([3, 1])
                            ac1.caption(f"{_dir_sym} ₹{_a['price']:,.2f}{_cc_badge}"
                                        + (f" · {_a['note']}" if _a.get('note') else ""))
                            if ac2.button("✕", key=f"alert_del_{sym}_{_i}"):
                                _all = _load_alerts()
                                _all[sym] = [x for x in _all.get(sym, [])
                                             if not (not x.get("triggered") and
                                                     x["price"] == _a["price"] and
                                                     x["direction"] == _a["direction"])]
                                _save_alerts(_all)
                                st.rerun()

            # ── Row 2: share actions (single popover) ─────────────────
            _share_col, = st.columns(1)
            with _share_col:
                with st.popover("📤 Share", use_container_width=True):
                    st.markdown(f"**Share signal — {sym}**")
                    real_ltp = quotes.get(sym, {}).get("last_price", ltp)
                    _sl   = round(real_ltp * (1 - INIT_SL_PCT  / 100), 2)
                    _tgt  = round(real_ltp * (1 + INIT_TGT_PCT / 100), 2)
                    _strategy = st.session_state.get("k_strategy", "Momentum")
                    if st.button("📨 Telegram", key=f"tg_{sym}",
                                 disabled=not _tg_enabled(),
                                 use_container_width=True):
                        tg_signal(sym, name, real_ltp, _sl, _tgt,
                                  row.get("vol_surge", 1.0), row.get("score", 50.0),
                                  _strategy, paper)
                        st.toast(f"📨 Signal sent for {sym}")
                    if st.button("🐦 X / Twitter", key=f"tw_{sym}",
                                 disabled=not _tw_enabled(),
                                 use_container_width=True):
                        if post_tweet(sym, name, real_ltp,
                                      row.get("pct_change", 0.0),
                                      row.get("vol_surge", 1.0),
                                      row.get("score", 50.0),
                                      _sl, _tgt, _strategy, paper):
                            st.toast(f"🐦 Tweeted {sym}!")
                        else:
                            st.toast(f"❌ Tweet failed for {sym}", icon="❌")
                    if st.button("📱 WhatsApp", key=f"wa_{sym}",
                                 disabled=not _wa_enabled(),
                                 use_container_width=True):
                        _wa_msg = (
                            f"MANIMINDSMONEY | RAMA\n"
                            f"{'[PAPER] ' if paper else ''}{sym} — {name}\n"
                            f"CMP ₹{real_ltp:,.2f}  Vol {row.get('vol_surge', 1.0):.1f}x  Score {row.get('score', 50.0):.0f}\n"
                            f"Target ₹{_tgt:,.2f}  SL ₹{_sl:,.2f}\n"
                            f"Strategy: {_strategy}"
                        )
                        if send_whatsapp(_wa_msg):
                            st.toast(f"📱 WhatsApp sent for {sym}!")
                        else:
                            st.toast(f"❌ WhatsApp failed for {sym}", icon="❌")


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    kite    = get_kite()
    init_positions(kite)       # restore from file + sync with Kite (once per session)
    account = fetch_account_info(kite)
    render_account_banner(account)
    check_telegram_commands(kite)   # respond to /pnl commands from Telegram

    universe = load_universe(UNIVERSE_CHOICE)

    # futures instruments — populated only for futures universe choices
    if UNIVERSE_CHOICE == "Futures – Current Month":
        _fut_inst = load_futures_instruments("current")
    elif UNIVERSE_CHOICE == "Futures – Next Month":
        _fut_inst = load_futures_instruments("next")
    else:
        _fut_inst: dict[str, dict] = {}
    lot_sizes = {s: v["lot_size"] for s, v in _fut_inst.items()} if _fut_inst else {}
    # store in session_state so render_screener_table can pass fut_info to do_buy
    st.session_state["_fut_instruments"] = _fut_inst

    with st.spinner(f"Loading fundamentals for {len(universe)} stocks (cached daily)…"):
        fund_df = load_fundamentals(tuple(universe))

    fund_pass, funnel = fundamental_filter(fund_df, enabled=USE_FUNDAMENTAL)
    # When filter is disabled, use full universe — fund_df may be incomplete
    # because _fetch_fund returns None for stocks where yfinance .info fails
    screened = fund_pass["symbol"].tolist() if USE_FUNDAMENTAL else list(universe)

    with st.spinner("Loading daily data — avg volumes + RSI (cached daily)…"):
        daily_data = load_daily_data(tuple(screened))

    if not daily_data:
        st.warning(
            "⚠️ Daily data (yfinance) unavailable — volume surge, RSI, EMA, and pattern signals "
            "are disabled. Momentum and Swing Trade strategies will show no results; "
            "strategy filters based on % change still work."
        )

    # include position symbols in quote fetch even if not in screened list
    pos_syms = [s for s, p in st.session_state.positions.items() if p["status"] == "open"]
    all_syms = list(dict.fromkeys(screened + pos_syms))

    # Cache quotes in session_state for 30 s so filter toggles don't re-hit Kite API
    _now        = time.time()
    _cache_age  = _now - st.session_state.get("_quotes_ts", 0)
    _cache_syms = st.session_state.get("_quotes_syms", [])
    if _cache_age > 30 or set(all_syms) != set(_cache_syms):
        with st.spinner(f"Fetching live quotes for {len(all_syms)} stocks via Kite…"):
            quotes = load_kite_quotes(kite, all_syms, fut_instruments=_fut_inst or None)
        st.session_state["_quotes_cache"] = quotes
        st.session_state["_quotes_ts"]    = _now
        st.session_state["_quotes_syms"]  = all_syms
    else:
        quotes = st.session_state["_quotes_cache"]

    # ── check price alerts on every refresh ──────────────────────────────────
    check_price_alerts(quotes)

    # ── order errors (persisted across rerun) ────────────────────────────────
    for err in st.session_state.pop("order_errors", []):
        st.error(f"🚨 {err}")

    # ── positions panel (above table) ─────────────────────────────────────────
    render_positions_panel(kite, quotes, PAPER_TRADE)

    # ── news data — always loaded (cached 2 h), shown in name popovers + filter ─
    with st.spinner("Loading news (cached 2 h)…"):
        news_data = load_news_data(tuple(screened))

    # ── 15-min VWAP — cached 5 min, used for VWAP filter and display ─────────
    with st.spinner("Loading 15-min VWAP (cached 5 min)…"):
        vwap_15_data = load_15min_vwap(tuple(screened))

    # ── screener table ────────────────────────────────────────────────────────
    intraday  = build_intraday_df(quotes, daily_data, news_data, vwap_15_data)
    tech_pass = technical_filter(
        intraday,
        use_pct=USE_PCT_FILTER,  min_pct=MIN_PCT_CHANGE,
        use_vol=USE_VOL_FILTER,  vol_min=VOL_SURGE_MIN,
        use_rsi=USE_RSI_FILTER,  rsi_min=RSI_MIN, rsi_max=RSI_MAX,
        use_vwap=USE_VWAP_FILTER, vwap_above=(VWAP_SIDE == "Above VWAP"),
        use_bs=USE_BS_FILTER, bs_min=BS_RATIO_MIN,
        use_bb_squeeze=USE_BB_SQUEEZE,
        use_nr7=USE_NR7,
        use_near_day_high=USE_NEAR_DAY_HIGH,
        use_tight_intraday=USE_TIGHT_INTRADAY,
        use_setup_rsi=USE_SETUP_RSI,
        use_inside_bar=USE_INSIDE_BAR,
        use_range_bo=USE_RANGE_BO,
        use_vcp=USE_VCP,
        use_news=USE_NEWS_FILTER,
        news_results_only=USE_RESULTS_FILTER,
        use_ema21=USE_EMA21,
        use_ema50=USE_EMA50,
        use_ema200=USE_EMA200,
    )
    final = score_df(tech_pass)
    if not final.empty:
        if "name" in fund_pass.columns:
            final = final.merge(fund_pass[["symbol", "name"]], on="symbol", how="left")
        # always ensure name column exists, fallback to symbol
        if "name" not in final.columns:
            final["name"] = final["symbol"]
        else:
            final["name"] = final["name"].fillna(final["symbol"])

    # ── active-filter info bar ───────────────────────────────────────────────
    _finfo = []
    if USE_PCT_FILTER:  _finfo.append(f"Chg≥{MIN_PCT_CHANGE}%")
    if USE_VOL_FILTER:  _finfo.append(f"Vol≥{VOL_SURGE_MIN}×")
    if USE_RSI_FILTER:  _finfo.append(f"RSI {RSI_MIN}-{RSI_MAX}")
    if USE_VWAP_FILTER: _finfo.append(VWAP_SIDE)
    if USE_BB_SQUEEZE:       _finfo.append("BB Squeeze")
    if USE_NR7:              _finfo.append("NR7")
    if USE_VCP:              _finfo.append("VCP")
    if USE_INSIDE_BAR:       _finfo.append("Inside Bar")
    if USE_RANGE_BO:         _finfo.append("Range BO")
    if USE_EMA21:            _finfo.append("Above EMA21")
    if USE_EMA50:            _finfo.append("Above EMA50")
    if USE_EMA200:           _finfo.append("Above EMA200")
    if USE_NEWS_FILTER:      _finfo.append("Positive news")
    if USE_RESULTS_FILTER:   _finfo.append("Recent results")
    _ftxt = " · ".join(_finfo) if _finfo else "no filters — all stocks shown"
    st.info(f"**{STRATEGY}** | active filters: {_ftxt} | matched: **{len(tech_pass)}** / {len(intraday)}")

    # Helpful tip when strategy returns 0 results
    _pattern_strategies = {"VCP Setup", "Inside Bar", "Pre-Breakout (coiling)"}
    if tech_pass.empty and intraday is not None and not intraday.empty:
        if STRATEGY in _pattern_strategies:
            st.warning(
                f"🔍 No stocks match the **{STRATEGY}** pattern today. "
                "These are low-frequency signals — try **Range Breakout (5-day)** or "
                "**Momentum (already moving)** for more results, or switch to "
                "**Custom (manual filters)** to relax individual conditions."
            )
        elif STRATEGY == "Swing Trade (3-5 day)":
            st.warning(
                "🔍 No stocks pass all Swing Trade filters (EMA alignment + RSI 50-72). "
                "Try disabling one EMA filter or widening the RSI range in the sidebar."
            )
        elif STRATEGY == "Range Breakout (5-day)":
            st.warning(
                "🔍 No range breakout stocks today. This strategy needs yesterday's close "
                "to exceed the 5-day high — fewer setups on sideways/down days."
            )

    # ── auto Telegram top-5 (fires at most every 5 min when top-5 changes) ───
    tg_top5(final, STRATEGY, PAPER_TRADE)

    # metrics
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Universe",         len(universe))
    c2.metric("Fundamental pass", len(screened))
    c3.metric("Live quotes",      len(quotes))
    c4.metric(f"{STRATEGY[:18]}",  len(final))
    c5.metric("Top score",        f"{final['score'].max():.0f}/100" if not final.empty else "—")

    st.markdown(
        f"<p style='text-align:right;color:#aaa;font-size:12px'>"
        f"⏱ {datetime.now():%Y-%m-%d %H:%M:%S} &nbsp;·&nbsp; "
        f"{'📄 PAPER TRADE' if PAPER_TRADE else '🔴 LIVE TRADE'}"
        f"</p>",
        unsafe_allow_html=True,
    )

    # futures banner — show expiry info + lot size reminder
    if lot_sizes:
        import datetime as _dt
        _lots_loaded = len(lot_sizes)
        st.info(
            f"📅 **Futures mode** — {_lots_loaded} stocks loaded · "
            f"{'Current' if 'Current' in UNIVERSE_CHOICE else 'Next'} month expiry · "
            f"Qty field defaults to lot size per stock (step = 1 lot). "
            f"Hover qty field to see lot size."
        )

    # ── Top-10 ticker strip ───────────────────────────────────────────────────
    if not final.empty:
        _top10 = final.nlargest(min(10, len(final)), "score")
        _chips = []
        for _, _r in _top10.iterrows():
            _pct  = float(_r.get("pct_change", 0.0))
            _col  = "#1b8a3e" if _pct >= 0 else "#c0392b"       # green / red
            _bg   = "#e8f5e9" if _pct >= 0 else "#fdecea"
            _sign = "▲" if _pct >= 0 else "▼"
            _chip = (
                f"<span style='background:{_bg};color:{_col};"
                f"padding:2px 8px;border-radius:12px;"
                f"font-size:12px;font-weight:600;white-space:nowrap'>"
                f"{_r['symbol']} {_sign}{abs(_pct):.1f}%</span>"
            )
            _chips.append(_chip)
        st.markdown(
            "<div style='display:flex;flex-wrap:wrap;gap:6px;"
            "padding:6px 0 10px 0;align-items:center'>"
            "<span style='font-size:11px;color:#888;margin-right:4px'>TOP&nbsp;10</span>"
            + " ".join(_chips) +
            "</div>",
            unsafe_allow_html=True,
        )

    render_screener_table(final, kite, quotes, PAPER_TRADE,
                          news_data=news_data, lot_sizes=lot_sizes)

    with st.expander("🔍 Filter funnel"):
        st.markdown(f"""
| Stage | Count |
|-------|-------|
| Universe | {funnel['total']} |
| After market cap ≥ {MARKET_CAP_MIN_CR:,.0f} Cr | {funnel['after_mktcap']} |
| After PE ≤ {PE_MAX} | {funnel['after_pe']} |
| After EPS ≥ {EPS_MIN} | {funnel['after_eps']} |
| After institutional ≥ {INST_MIN_PCT:.0f}% | {funnel['after_inst']} |
| After technical filter | {len(final)} |
        """)

    with st.expander("📊 Score breakdown"):
        st.markdown("""
| Component | Weight | Basis |
|-----------|--------|-------|
| Volume Score | 0–40 | Volume ÷ 20-day avg |
| Momentum Score | 0–40 | % price change vs prev close |
| Resistance Score | 0–20 | Closeness to 52-week high |
| **Total** | **0–100** | |
        """)


def _check_login() -> bool:
    """Show login wall. Returns True if authenticated."""
    if st.session_state.get("_rama_auth"):
        return True

    st.set_page_config(page_title="RAMA — Login", page_icon="🏦", layout="centered")
    st.markdown(
        "<h2 style='text-align:center;margin-bottom:4px'>🏦 MANIMINDSMONEY</h2>"
        "<p style='text-align:center;color:#888;margin-top:0'>RAMA Screener</p>",
        unsafe_allow_html=True,
    )
    st.divider()

    with st.form("login_form"):
        username = st.text_input("Username", placeholder="Enter username")
        password = st.text_input("Password", placeholder="Enter password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True, type="primary")

    if submitted:
        _valid_user = os.getenv("RAMA_USERNAME", "manideep")
        _valid_pass = os.getenv("RAMA_PASSWORD", "28061991")
        if username.strip() == _valid_user and password.strip() == _valid_pass:
            st.session_state["_rama_auth"] = True
            st.rerun()
        else:
            st.error("Invalid username or password.")
    return False


# Login disabled — uncomment to re-enable:
# if _check_login():
#     main()
main()

# ── auto-refresh (non-blocking) ───────────────────────────────────────────────
# st_autorefresh triggers a client-side page reload every REFRESH_SECONDS.
# Unlike time.sleep(), it does NOT block the Python process, so filter and
# widget interactions respond instantly at any point during the interval.
st_autorefresh(interval=REFRESH_SECONDS * 1000, key="screener_autorefresh")
