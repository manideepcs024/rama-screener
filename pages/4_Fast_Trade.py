"""
Fast Trading Page — LOCAL ONLY. Do NOT push to Streamlit Cloud.
Nifty & Sensex intraday option trading with Paper / Real mode.

Security: add your server's public IP to Zerodha API console → allowed IPs.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from kiteconnect import KiteConnect  # type: ignore

# ── constants ─────────────────────────────────────────────────────────────────
_API_KEY    = os.getenv("ZERODHA_API_KEY", "")
_TOKEN_FILE = ROOT / "output" / "kite_token.json"

LOT_SIZES   = {"NIFTY": 50, "SENSEX": 20}
STRIKE_STEP = {"NIFTY": 50,  "SENSEX": 100}
SEGMENTS    = {"NIFTY": "NFO", "SENSEX": "BFO"}
SPOT_SYMS   = {"NIFTY": "NSE:NIFTY 50", "SENSEX": "BSE:SENSEX"}
YF_SYMS     = {"NIFTY": "^NSEI",         "SENSEX": "^BSESN"}
COLORS      = {"NIFTY": "#1a73e8",        "SENSEX": "#7b1fa2"}
BADGES      = {"NIFTY": "N",              "SENSEX": "S"}

SL_DEFAULT_PCT  = 10.0   # % loss to place SL
TGT_DEFAULT_PCT = 10.0   # % gain for target
TRAIL_LEVELS    = [10, 7, 5, 2, 0, -5]   # SL % below entry per trail click (neg = profit lock)
TARGET_LEVELS   = [10, 15, 20]           # target % per click


# ── auth ─────────────────────────────────────────────────────────────────────

def _load_token() -> str:
    try:
        return json.loads(_TOKEN_FILE.read_text()).get("token", "")
    except Exception:
        return os.getenv("ZERODHA_ACCESS_TOKEN", "")


def _make_kite() -> KiteConnect:
    kite = KiteConnect(api_key=_API_KEY)
    t = _load_token()
    if t:
        kite.set_access_token(t)
    return kite


def get_kite() -> KiteConnect:
    kite = _make_kite()
    try:
        kite.profile()
        return kite
    except Exception:
        st.error("Kite session expired — log in from the main screener page.")
        st.stop()


# ── instruments (cached 1 hr) ─────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _instruments(seg: str) -> list[dict]:
    try:
        return _make_kite().instruments(seg)
    except Exception:
        return []


def _expiries(seg: str, name: str) -> list[date]:
    """Returns sorted list of upcoming weekly expiries (up to 4)."""
    today = date.today()
    exp = sorted({
        i["expiry"]
        for i in _instruments(seg)
        if i["name"] == name
        and i["instrument_type"] in ("CE", "PE")
        and i["expiry"] >= today
    })
    return exp[:4]


def _opt_tradingsymbol(seg: str, name: str, expiry: date,
                        strike: float, opt: str) -> Optional[str]:
    for i in _instruments(seg):
        if (i["name"] == name and i["instrument_type"] == opt
                and i["expiry"] == expiry and i["strike"] == strike):
            return i["tradingsymbol"]
    return None


def _opt_token(seg: str, name: str, expiry: date,
               strike: float, opt: str) -> Optional[int]:
    for i in _instruments(seg):
        if (i["name"] == name and i["instrument_type"] == opt
                and i["expiry"] == expiry and i["strike"] == strike):
            return int(i["instrument_token"])
    return None


# ── market data ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=5, show_spinner=False)
def _ltp_batch(symbols: tuple[str, ...]) -> dict[str, float]:
    try:
        q = _make_kite().ltp(list(symbols))
        return {k: float(v["last_price"]) for k, v in q.items()}
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def _daily_ta(yf_sym: str) -> dict:
    try:
        import yfinance as yf
        df = yf.download(yf_sym, period="60d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        try:
            import pandas_ta as pta
            adx_df  = pta.adx(high, low, close, length=14)
            adx_val = float(adx_df["ADX_14"].iloc[-1]) if adx_df is not None else 20.0
            rsi_val = float(pta.rsi(close, length=14).iloc[-1])
            ema9    = float(pta.ema(close, length=9).iloc[-1])
            ema21   = float(pta.ema(close, length=21).iloc[-1])
        except Exception:
            adx_val = 20.0; rsi_val = 50.0
            ema9 = ema21 = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else float(close.iloc[-1])
        cur  = float(close.iloc[-1])
        bull = sum([cur > ema9, ema9 > ema21, rsi_val > 55])
        bear = sum([cur < ema9, ema9 < ema21, rsi_val < 45])
        if adx_val >= 25:
            direction = "BULLISH" if bull > bear else ("BEARISH" if bear > bull else "NEUTRAL")
        else:
            direction = "NEUTRAL"
        return {
            "adx": adx_val, "rsi": rsi_val, "ema9": ema9, "ema21": ema21,
            "direction": direction, "pct": (cur - prev) / prev * 100,
            "is_trending": adx_val >= 25,
        }
    except Exception:
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def _intraday_15m(token: int) -> dict:
    """
    Returns VWAP, proximity label, and detected patterns from 15-min candles.
    Cached 60 s — fast enough for active trading.
    """
    empty = {"vwap": None, "df_len": 0, "patterns": [], "range_pct": None}
    try:
        today   = date.today()
        from_dt = datetime.combine(today, dtime(9, 15))
        to_dt   = datetime.now()
        raw     = _make_kite().historical_data(token, from_dt, to_dt, "15minute")
        if not raw:
            return empty
        df = pd.DataFrame(raw)
        df.columns = [c.lower() for c in df.columns]
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"])
        if df.empty:
            return empty

        # VWAP
        vwap = None
        if df["volume"].sum() > 0:
            tp   = (df["high"] + df["low"] + df["close"]) / 3
            vwap = float((tp * df["volume"]).cumsum().iloc[-1]
                         / df["volume"].cumsum().iloc[-1])

        # Day range % (high - low of all candles so far)
        day_high = float(df["high"].max())
        day_low  = float(df["low"].min())
        range_pct = (day_high - day_low) / day_low * 100 if day_low else None

        # Pattern detection (needs at least 4 bars)
        patterns: list[tuple[str, str, str]] = []
        if len(df) >= 4:
            h = df["high"].values
            l = df["low"].values
            r = h - l                   # bar ranges

            # Inside Bar — last candle contained within previous
            if h[-1] < h[-2] and l[-1] > l[-2]:
                patterns.append(("Inside Bar", "Coiling — breakout pending", "#e65100"))

            # NR4 — narrowest range in last 4 bars
            if r[-1] == min(r[-4:]):
                patterns.append(("NR4", "Tightest range in 4 bars — explosive move near", "#7b1fa2"))

            # VCP — 4 consecutive contracting ranges
            if r[-4] > r[-3] > r[-2] > r[-1]:
                patterns.append(("VCP", "Volatility contraction — potential breakout", "#1a73e8"))

            # Range Compression — last 2 bars both inside the bar before them
            if (h[-1] < h[-3] and l[-1] > l[-3] and
                    h[-2] < h[-3] and l[-2] > l[-3]):
                patterns.append(("Range Compress", "2-bar squeeze inside mother bar", "#00695c"))

        return {
            "vwap":      vwap,
            "df_len":    len(df),
            "patterns":  patterns,
            "range_pct": range_pct,
            "day_high":  day_high,
            "day_low":   day_low,
        }
    except Exception:
        return empty


def _vwap_proximity(ltp: Optional[float], vwap: Optional[float]) -> tuple[str, str]:
    """Returns (label, color) describing how far LTP is from VWAP."""
    if ltp is None or vwap is None or vwap == 0:
        return "VWAP N/A", "#aaa"
    diff_pct = (ltp - vwap) / vwap * 100
    abs_pct  = abs(diff_pct)
    side     = "above" if diff_pct >= 0 else "below"
    color    = "#1b5e20" if diff_pct >= 0 else "#b71c1c"
    if abs_pct <= 0.5:
        return f"At VWAP ({diff_pct:+.1f}%)", "#e65100"
    if abs_pct <= 1.5:
        return f"Near VWAP {side} ({diff_pct:+.1f}%)", color
    if abs_pct <= 3.0:
        return f"Away from VWAP {side} ({diff_pct:+.1f}%)", color
    return f"Far from VWAP {side} ({diff_pct:+.1f}%)", color


# ── index intraday VWAP (yfinance 15-min, cached 60s) ────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def _index_intraday(yf_sym: str) -> dict:
    try:
        import yfinance as yf
        df = yf.download(yf_sym, period="1d", interval="15m",
                         progress=False, auto_adjust=True)
        if df.empty:
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        vol   = df["Volume"].squeeze()
        tp    = (high + low + close) / 3
        vwap  = float((tp * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1]) if vol.sum() > 0 \
                else float(close.mean())
        return {"vwap": vwap, "close": float(close.iloc[-1])}
    except Exception:
        return {}


# ── multi-strategy signal engine ──────────────────────────────────────────────

def _compute_signals(spot: float, ta: dict, ce_ltp, pe_ltp,
                     ce_data: dict, pe_data: dict, idx_vwap) -> dict:
    """
    Scores 6 strategies and returns a consolidated BUY CE / BUY PE / WAIT recommendation.
    Returns dict with keys: signals, score, recommendation, rec_color, confidence, ce_score, pe_score.
    """
    # Each entry: (description, vote)  vote: +ve = CE, -ve = PE, 0 = neutral
    sigs: list[tuple[str, int, str]] = []

    adx       = ta.get("adx", 20)
    direction = ta.get("direction", "NEUTRAL")
    is_trend  = ta.get("is_trending", False)
    rsi       = ta.get("rsi", 50)
    ce_vwap   = ce_data.get("vwap")
    pe_vwap   = pe_data.get("vwap")
    ce_pats   = ce_data.get("patterns", [])
    pe_pats   = pe_data.get("patterns", [])

    # ── 1. Daily Trend (ADX + EMA) ────────────────────────────────────────────
    if not is_trend:
        sigs.append(("CHOPPY — ADX<25, avoid directional buying", 0, "#888"))
    elif direction == "BULLISH":
        w = 2 if adx >= 30 else 1
        sigs.append((f"Daily BULLISH trend (ADX {adx:.0f})", +w, "#1b5e20"))
    elif direction == "BEARISH":
        w = 2 if adx >= 30 else 1
        sigs.append((f"Daily BEARISH trend (ADX {adx:.0f})", -w, "#b71c1c"))
    else:
        sigs.append(("Daily trend NEUTRAL", 0, "#888"))

    # ── 2. RSI Momentum ───────────────────────────────────────────────────────
    if rsi >= 70:
        sigs.append((f"RSI {rsi:.0f} — Overbought, CE risky", -1, "#b71c1c"))
    elif rsi <= 30:
        sigs.append((f"RSI {rsi:.0f} — Oversold, PE risky", +1, "#1b5e20"))
    elif rsi > 55:
        sigs.append((f"RSI {rsi:.0f} — Positive momentum", +1, "#1b5e20"))
    elif rsi < 45:
        sigs.append((f"RSI {rsi:.0f} — Negative momentum", -1, "#b71c1c"))
    else:
        sigs.append((f"RSI {rsi:.0f} — Neutral zone", 0, "#888"))

    # ── 3. Index intraday VWAP ────────────────────────────────────────────────
    if idx_vwap and spot > 0:
        diff_pct = (spot - idx_vwap) / idx_vwap * 100
        if diff_pct > 0.3:
            sigs.append((f"Index above intraday VWAP ({diff_pct:+.1f}%)", +1, "#1b5e20"))
        elif diff_pct < -0.3:
            sigs.append((f"Index below intraday VWAP ({diff_pct:+.1f}%)", -1, "#b71c1c"))
        else:
            sigs.append((f"Index at intraday VWAP ({diff_pct:+.1f}%) — no bias", 0, "#e65100"))

    # ── 4. CE option vs its VWAP ──────────────────────────────────────────────
    if ce_ltp and ce_vwap:
        pts = ce_ltp - ce_vwap
        if pts > 0:
            sigs.append((f"CE above its VWAP (+{pts:.1f}pts) — CE gaining", +1, "#1b5e20"))
        else:
            sigs.append((f"CE below its VWAP ({pts:.1f}pts) — CE weakening", -1, "#b71c1c"))

    # ── 5. PE option vs its VWAP (inverse: PE rising = bearish for index) ────
    if pe_ltp and pe_vwap:
        pts = pe_ltp - pe_vwap
        if pts > 0:
            sigs.append((f"PE above its VWAP (+{pts:.1f}pts) — PE accumulating (bearish)", -1, "#b71c1c"))
        else:
            sigs.append((f"PE below its VWAP ({pts:.1f}pts) — PE weakening (bullish)", +1, "#1b5e20"))

    # ── 6. Breakout patterns on options ──────────────────────────────────────
    if ce_pats:
        names = " + ".join(p[0] for p in ce_pats)
        sigs.append((f"CE pattern: {names} → breakout setup", +1, "#1a73e8"))
    if pe_pats:
        names = " + ".join(p[0] for p in pe_pats)
        sigs.append((f"PE pattern: {names} → breakout setup", -1, "#7b1fa2"))

    # ── Score & Recommendation ────────────────────────────────────────────────
    score    = sum(s[1] for s in sigs)
    ce_score = sum(s[1] for s in sigs if s[1] > 0)
    pe_score = sum(abs(s[1]) for s in sigs if s[1] < 0)

    if not is_trend:
        rec, rc, conf = "WAIT — Choppy Market", "#888", "LOW"
    elif score >= 4:
        rec, rc, conf = "STRONG BUY CE", "#1b5e20", "HIGH"
    elif score >= 2:
        rec, rc, conf = "BUY CE", "#2e7d32", "MEDIUM"
    elif score >= 1:
        rec, rc, conf = "LEAN CE", "#388e3c", "LOW"
    elif score <= -4:
        rec, rc, conf = "STRONG BUY PE", "#b71c1c", "HIGH"
    elif score <= -2:
        rec, rc, conf = "BUY PE", "#c62828", "MEDIUM"
    elif score <= -1:
        rec, rc, conf = "LEAN PE", "#d32f2f", "LOW"
    else:
        rec, rc, conf = "WAIT — Mixed Signals", "#e65100", "LOW"

    return {
        "signals":    sigs,
        "score":      score,
        "ce_score":   ce_score,
        "pe_score":   pe_score,
        "recommendation": rec,
        "rec_color":  rc,
        "confidence": conf,
        "total":      len(sigs),
    }


# ── order helpers ─────────────────────────────────────────────────────────────

def _place_market_order(kite: KiteConnect, exchange: str, tradingsymbol: str,
                         txn: str, qty: int, paper: bool) -> str:
    if paper:
        return f"PAPER_{int(time.time())}"
    oid = kite.place_order(
        variety="regular",
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        transaction_type=txn,
        quantity=qty,
        product="MIS",
        order_type="MARKET",
    )
    return str(oid)


def _place_slm_order(kite: KiteConnect, exchange: str, tradingsymbol: str,
                      qty: int, trigger: float, paper: bool) -> str:
    if paper:
        return f"PAPER_SL_{int(time.time())}"
    oid = kite.place_order(
        variety="regular",
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        transaction_type="SELL",
        quantity=qty,
        product="MIS",
        order_type="SL-M",
        trigger_price=round(trigger, 1),
    )
    return str(oid)


def _cancel_order(kite: KiteConnect, order_id: str, paper: bool):
    if paper or order_id.startswith("PAPER"):
        return
    try:
        kite.cancel_order(variety="regular", order_id=order_id)
    except Exception:
        pass


def _modify_slm(kite: KiteConnect, order_id: str, new_trigger: float, paper: bool):
    if paper or order_id.startswith("PAPER"):
        return
    try:
        kite.modify_order(
            variety="regular",
            order_id=order_id,
            trigger_price=round(new_trigger, 1),
        )
    except Exception as e:
        st.warning(f"Could not modify SL: {e}")


def _get_open_qty(kite: KiteConnect, tradingsymbol: str, paper: bool) -> int:
    """Returns net quantity from day positions. 0 = no open position."""
    if paper:
        return -1  # paper: trust session_state, return sentinel
    try:
        positions = kite.positions()
        for p in positions.get("day", []):
            if p["tradingsymbol"] == tradingsymbol:
                return int(p["quantity"])
        return 0
    except Exception:
        return -1  # on error keep existing state


def _get_fill_price(kite: KiteConnect, order_id: str, paper: bool,
                    fallback: float) -> float:
    if paper or order_id.startswith("PAPER"):
        return fallback
    try:
        for o in kite.orders():
            if str(o["order_id"]) == order_id and o["status"] == "COMPLETE":
                return float(o["average_price"])
    except Exception:
        pass
    return fallback


# ── session state helpers ─────────────────────────────────────────────────────

def _init_state():
    if "ft_positions" not in st.session_state:
        st.session_state["ft_positions"] = {}   # {idx_name: position_dict}
    if "ft_paper" not in st.session_state:
        st.session_state["ft_paper"] = True


def _pos(idx_name: str) -> dict:
    return st.session_state["ft_positions"].get(idx_name, {})


def _set_pos(idx_name: str, d: dict):
    st.session_state["ft_positions"][idx_name] = d


def _clear_pos(idx_name: str):
    st.session_state["ft_positions"].pop(idx_name, None)


# ── public IP ─────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _public_ip() -> str:
    try:
        import requests
        return requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:
        return "unavailable"


# ════════════════════════════════════════════════════════════════════════════
# PAGE
# ════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Fast Trade",
    page_icon="zap",
    layout="wide",
)
from _auth import require_login; require_login()

try:
    from streamlit_autorefresh import st_autorefresh  # type: ignore
    st_autorefresh(interval=5_000, key="ft_refresh")
except ImportError:
    pass

_init_state()
kite = get_kite()
paper: bool = st.session_state["ft_paper"]

st.markdown("""
<style>
.ft-title { font-size:24px;font-weight:900;letter-spacing:2px;color:#1a73e8; }
.idx-card {
    border:2px solid #e0e0e0;border-radius:14px;padding:16px 18px;
    background:#fff;box-shadow:0 2px 8px rgba(0,0,0,0.08);
}
.idx-badge {
    display:inline-flex;align-items:center;justify-content:center;
    width:30px;height:30px;border-radius:8px;font-weight:800;
    font-size:14px;color:#fff;margin-right:8px;
}
.pos-card {
    border:2px solid;border-radius:12px;padding:14px 18px;margin-top:10px;
}
.pnl-pos { font-size:20px;font-weight:800;color:#1b5e20; }
.pnl-neg { font-size:20px;font-weight:800;color:#b71c1c; }
.sl-line  { font-size:13px;padding:4px 0; }
.ip-banner {
    background:#fff8e1;border:1px solid #ffd54f;border-radius:8px;
    padding:10px 16px;font-size:13px;margin-bottom:14px;
}
/* Remove Streamlit's default -1rem bottom margin on markdown containers */
div[data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
div[data-testid="stMarkdownContainer"] > p { margin-bottom: 0 !important; }
div[data-testid="stMarkdownContainer"] > div { margin-bottom: 0 !important; }
</style>
""", unsafe_allow_html=True)

# ── top bar ───────────────────────────────────────────────────────────────────
hdr_l, hdr_r = st.columns([3, 1])
with hdr_l:
    ip = _public_ip()
    mode_badge = (
        "<span style='background:#ff6f00;color:#fff;padding:3px 10px;"
        "border-radius:5px;font-weight:700;font-size:13px'>PAPER MODE</span>"
        if paper else
        "<span style='background:#b71c1c;color:#fff;padding:3px 10px;"
        "border-radius:5px;font-weight:700;font-size:13px'>LIVE TRADING</span>"
    )
    st.markdown(
        f"<span class='ft-title'>⚡ Fast Trade</span> &nbsp; {mode_badge} &nbsp;"
        f"<span style='font-size:12px;color:#888'>"
        f"Auto-refresh 5s &nbsp;|&nbsp; {datetime.now().strftime('%H:%M:%S')}</span>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='ip-banner'>"
        f"<b>Your public IP: {ip}</b> &nbsp;—&nbsp; "
        f"Add this in Zerodha API Console → <i>Edit App → Redirect URL & IP</i> "
        f"to avoid login errors.</div>",
        unsafe_allow_html=True,
    )

with hdr_r:
    st.markdown("<br>", unsafe_allow_html=True)
    new_mode = st.toggle("Live Trading", value=not paper, key="ft_mode_toggle")
    if new_mode == paper:   # changed
        if new_mode:        # switching to live
            st.session_state["ft_paper"] = False
        else:               # switching to paper
            st.session_state["ft_paper"] = True
        st.rerun()

st.markdown("---")

# ── fetch all spot prices up-front ────────────────────────────────────────────
spot_syms = tuple(SPOT_SYMS.values())
spot_q    = _ltp_batch(spot_syms)

# ── two-column layout ─────────────────────────────────────────────────────────
col_nifty, col_sensex = st.columns(2)

for idx_name, container in [("NIFTY", col_nifty), ("SENSEX", col_sensex)]:
    with container:
        seg  = SEGMENTS[idx_name]
        step = STRIKE_STEP[idx_name]
        lot  = LOT_SIZES[idx_name]
        col  = COLORS[idx_name]
        badge= BADGES[idx_name]

        spot = spot_q.get(SPOT_SYMS[idx_name], 0.0)
        if spot <= 0:
            st.warning(f"{idx_name}: spot unavailable")
            continue

        atm  = round(spot / step) * step
        pct  = _daily_ta(YF_SYMS[idx_name]).get("pct", 0.0)
        ta   = _daily_ta(YF_SYMS[idx_name])

        # expiry selector
        exp_list = _expiries(seg, idx_name)
        exp_key  = f"ft_exp_{idx_name}"
        if exp_list:
            exp_labels = []
            for i, e in enumerate(exp_list):
                label = e.strftime("%d %b")
                if i == 0:   label += " (This week)"
                elif i == 1: label += " (Next week)"
                exp_labels.append(label)
            chosen_idx = st.radio(
                "Expiry", options=range(len(exp_list)),
                format_func=lambda i: exp_labels[i],
                index=st.session_state.get(exp_key, 0),
                key=exp_key,
                horizontal=True,
                label_visibility="collapsed",
            )
            expiry = exp_list[chosen_idx]
        else:
            expiry = None

        # get ATM option LTPs
        ce_sym = _opt_tradingsymbol(seg, idx_name, expiry, atm, "CE") if expiry else None
        pe_sym = _opt_tradingsymbol(seg, idx_name, expiry, atm, "PE") if expiry else None
        ce_tok = _opt_token(seg, idx_name, expiry, atm, "CE")        if expiry else None
        pe_tok = _opt_token(seg, idx_name, expiry, atm, "PE")        if expiry else None

        opt_syms: list[str] = []
        if ce_sym: opt_syms.append(f"{seg}:{ce_sym}")
        if pe_sym: opt_syms.append(f"{seg}:{pe_sym}")
        opt_q = _ltp_batch(tuple(opt_syms)) if opt_syms else {}

        ce_ltp = opt_q.get(f"{seg}:{ce_sym}") if ce_sym else None
        pe_ltp = opt_q.get(f"{seg}:{pe_sym}") if pe_sym else None

        # VWAP + patterns for CE and PE
        ce_data  = _intraday_15m(ce_tok) if ce_tok else {}
        pe_data  = _intraday_15m(pe_tok) if pe_tok else {}
        ce_vwap  = ce_data.get("vwap")
        pe_vwap  = pe_data.get("vwap")
        ce_pats  = ce_data.get("patterns", [])
        pe_pats  = pe_data.get("patterns", [])
        ce_prox, ce_prox_col = _vwap_proximity(ce_ltp, ce_vwap)
        pe_prox, pe_prox_col = _vwap_proximity(pe_ltp, pe_vwap)

        # index intraday VWAP + multi-strategy signals
        idx_intra  = _index_intraday(YF_SYMS[idx_name])
        idx_vwap   = idx_intra.get("vwap")
        sig_result = _compute_signals(spot, ta, ce_ltp, pe_ltp,
                                      ce_data, pe_data, idx_vwap)

        def _vwap_html(ltp, vwap, label, prox, prox_col, pats, day_data):
            if not ltp:
                return f"<span style='color:#aaa'>{label} N/A</span>"
            opt_col = "#1b5e20" if label == "CE" else "#b71c1c"
            ltp_col = "#1b5e20" if (vwap and ltp >= vwap) else "#b71c1c"
            html = (
                f"<b style='color:{opt_col}'>{label}</b> "
                f"<b style='color:{ltp_col}'>&#8377;{ltp:.2f}</b>"
            )
            if vwap:
                pts  = ltp - vwap
                sign = "+" if pts >= 0 else ""
                html += (
                    f" <span style='color:#888;font-size:11px'>"
                    f"VWAP &#8377;{vwap:.2f} ({sign}{pts:.1f}pts)</span>"
                    f" <span style='color:{prox_col};font-size:11px;font-weight:600'>"
                    f"[{prox}]</span>"
                )
            dh = day_data.get("day_high"); dl = day_data.get("day_low")
            rp = day_data.get("range_pct")
            if dh and dl and rp is not None:
                html += (
                    f"<br><span style='color:#888;font-size:11px'>"
                    f"Range: &#8377;{dl:.1f}–&#8377;{dh:.1f} ({rp:.1f}%)</span>"
                )
            if pats:
                pills = " ".join(
                    f"<span style='display:inline-block;padding:1px 7px;"
                    f"border-radius:4px;font-size:10px;font-weight:700;"
                    f"border:1px solid {c};color:{c}'>{n}</span>"
                    for n, _, c in pats
                )
                html += f" &nbsp; {pills}"
            return html

        # Trend badge
        direction = ta.get("direction", "NEUTRAL")
        is_trend  = ta.get("is_trending", False)
        adx_v     = ta.get("adx", 20)
        rsi_v     = ta.get("rsi", 50)
        if not is_trend:
            trend_html = "<span style='color:#b71c1c;font-weight:700'>CHOPPY (ADX&lt;25)</span>"
        elif direction == "BULLISH":
            trend_html = "<span style='color:#1b5e20;font-weight:700'>▲ TRENDING BULLISH</span>"
        elif direction == "BEARISH":
            trend_html = "<span style='color:#b71c1c;font-weight:700'>▼ TRENDING BEARISH</span>"
        else:
            trend_html = "<span style='color:#e65100;font-weight:700'>— NEUTRAL</span>"

        p_col  = "#1b5e20" if pct >= 0 else "#b71c1c"
        p_arr  = "▲" if pct >= 0 else "▼"

        # ── card header ───────────────────────────────────────────────────────
        st.markdown(
            f"<div style='display:flex;align-items:center;margin-bottom:8px'>"
            f"<span class='idx-badge' style='background:{col}'>{badge}</span>"
            f"<span style='font-size:17px;font-weight:800'>{idx_name}</span>"
            f"<span style='font-size:20px;font-weight:800;margin-left:10px'>"
            f"&#8377;{spot:,.2f}</span>"
            f"<span style='font-size:13px;font-weight:600;margin-left:6px;color:{p_col}'>"
            f"{p_arr}{abs(pct):.2f}%</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # ATM + CE/PE LTP + VWAP
        exp_str = expiry.strftime("%d %b") if expiry else "—"
        week_lbl = exp_labels[chosen_idx] if exp_list else exp_str
        st.markdown(
            f"<div style='font-size:12px;color:#555;margin-bottom:4px'>"
            f"ATM <b>{atm:,.0f}</b> &nbsp;|&nbsp; <b>{week_lbl}</b></div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div style='font-size:13px;line-height:2.2'>"
            f"{_vwap_html(ce_ltp, ce_vwap, 'CE', ce_prox, ce_prox_col, ce_pats, ce_data)}<br>"
            f"{_vwap_html(pe_ltp, pe_vwap, 'PE', pe_prox, pe_prox_col, pe_pats, pe_data)}"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div style='font-size:12px;margin-top:4px'>"
            f"ADX <b>{adx_v:.0f}</b> &nbsp; RSI <b>{rsi_v:.0f}</b>"
            f" &nbsp;|&nbsp; {trend_html}</div>",
            unsafe_allow_html=True,
        )

        st.markdown("<hr style='margin:10px 0'>", unsafe_allow_html=True)

        # ── buy controls ──────────────────────────────────────────────────────
        pos = _pos(idx_name)

        # sync position with Kite (detect manual close)
        if pos.get("active"):
            live_qty = _get_open_qty(kite, pos["tradingsymbol"], paper)
            if live_qty == 0:  # manually closed
                # cancel any pending SL order
                if pos.get("sl_order_id"):
                    _cancel_order(kite, pos["sl_order_id"], paper)
                _clear_pos(idx_name)
                st.toast(f"⚠️ {idx_name} position was manually closed in Zerodha — delinked.")
                pos = {}

        # ── signal panel (always visible — even during open position) ──────────
        sr    = sig_result
        conf  = sr["confidence"]
        conf_badge = {
            "HIGH":   ("HIGH CONFIDENCE", "#1b5e20", "#e8f5e9"),
            "MEDIUM": ("MEDIUM CONFIDENCE", "#e65100", "#fff3e0"),
            "LOW":    ("LOW CONFIDENCE",  "#888",    "#f5f5f5"),
        }.get(conf, ("", "#888", "#f5f5f5"))

        st.markdown(
            f"<div style='border:2px solid {sr['rec_color']};border-radius:10px;"
            f"padding:12px 16px;margin:6px 0;background:#fafafa'>"
            f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px'>"
            f"<span style='font-size:17px;font-weight:900;color:{sr['rec_color']}'>"
            f"{sr['recommendation']}</span>"
            f"<span style='font-size:11px;font-weight:700;padding:2px 8px;border-radius:5px;"
            f"color:{conf_badge[1]};background:{conf_badge[2]}'>{conf_badge[0]}</span>"
            f"<span style='font-size:11px;color:#888;margin-left:auto'>"
            f"CE +{sr['ce_score']} &nbsp; PE -{sr['pe_score']} &nbsp; Net: {sr['score']:+d}"
            f"</span></div>"
            + "".join(
                f"<div style='font-size:11px;line-height:1.9;color:{c}'>"
                f"{'✓' if v > 0 else ('✗' if v < 0 else '—')} {desc}</div>"
                for desc, v, c in sr["signals"]
            )
            + "</div>",
            unsafe_allow_html=True,
        )

        st.markdown("<hr style='margin:8px 0'>", unsafe_allow_html=True)

        if not pos.get("active"):
            # ── BUY card ──────────────────────────────────────────────────────
            st.markdown(
                "<div style='border:1px solid #e0e0e0;border-radius:10px;"
                "padding:14px 16px;background:#fff;margin-bottom:6px'>"
                "<div style='font-size:12px;font-weight:700;color:#555;"
                "text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px'>"
                "Place Order</div>",
                unsafe_allow_html=True,
            )

            # ── no open position → show buy form ──────────────────────────────
            bc1, bc2 = st.columns([1, 1])
            with bc1:
                lots_key = f"ft_lots_{idx_name}"
                if lots_key not in st.session_state:
                    st.session_state[lots_key] = 1
                lots_val = st.number_input(
                    f"Lots (1 lot = {lot} qty)",
                    min_value=1, max_value=50,
                    value=st.session_state[lots_key],
                    step=1,
                    key=lots_key,
                )
                qty = lots_val * lot
                st.markdown(
                    f"<div style='font-size:12px;color:#888'>Total qty: <b>{qty}</b></div>",
                    unsafe_allow_html=True,
                )

            # margin display
            ce_margin = round(ce_ltp * qty, 2) if ce_ltp else None
            pe_margin = round(pe_ltp * qty, 2) if pe_ltp else None

            def _fmt_inr(v):
                if v is None: return "N/A"
                if v >= 100_000: return f"₹{v/100_000:.2f}L"
                return f"₹{v:,.0f}"

            st.markdown(
                f"<div style='background:#f8f9ff;border:1px solid #e3e8ff;"
                f"border-radius:8px;padding:8px 12px;margin:6px 0;font-size:12px'>"
                f"<b>Margin required ({qty} qty)</b><br>"
                f"<span style='color:#1b5e20'>CE: <b>{_fmt_inr(ce_margin)}</b></span>"
                f"&nbsp;&nbsp;"
                f"<span style='color:#b71c1c'>PE: <b>{_fmt_inr(pe_margin)}</b></span>"
                f"<span style='color:#aaa;font-size:10px'> (premium × qty)</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

            mode_note = "PAPER" if paper else "LIVE"
            with bc2:
                st.markdown(f"<div style='font-size:11px;color:#888;margin-bottom:4px'>"
                            f"{mode_note} order on click</div>", unsafe_allow_html=True)
                buy_ce = st.button(
                    f"BUY CE ▲",
                    key=f"buy_ce_{idx_name}",
                    type="primary",
                    use_container_width=True,
                    disabled=not bool(ce_sym and ce_ltp),
                )
                buy_pe = st.button(
                    f"BUY PE ▼",
                    key=f"buy_pe_{idx_name}",
                    use_container_width=True,
                    disabled=not bool(pe_sym and pe_ltp),
                )

            for opt_type, do_buy, opt_sym, entry_ltp in [
                ("CE", buy_ce, ce_sym, ce_ltp),
                ("PE", buy_pe, pe_sym, pe_ltp),
            ]:
                if not do_buy:
                    continue
                try:
                    oid = _place_market_order(
                        kite, seg, opt_sym, "BUY", qty, paper
                    )
                    # wait briefly for fill then get actual price
                    if not paper:
                        time.sleep(1.2)
                    fill = _get_fill_price(kite, oid, paper, entry_ltp)
                    sl   = round(fill * (1 - SL_DEFAULT_PCT / 100), 1)
                    tgt  = round(fill * (1 + TGT_DEFAULT_PCT / 100), 1)
                    # place SL-M order immediately
                    sl_oid = _place_slm_order(kite, seg, opt_sym, qty, sl, paper)
                    _set_pos(idx_name, {
                        "active":        True,
                        "tradingsymbol": opt_sym,
                        "exchange":      seg,
                        "type":          opt_type,
                        "lots":          lots_val,
                        "qty":           qty,
                        "remaining_qty": qty,
                        "entry_price":   fill,
                        "sl":            sl,
                        "target":        tgt,
                        "sl_order_id":   sl_oid,
                        "buy_order_id":  oid,
                        "trail_level":   0,
                        "target_level":  0,
                        "partial_done":  False,
                        "atm":           atm,
                    })
                    mode_str = "PAPER" if paper else "LIVE"
                    st.success(
                        f"{mode_str} BUY {opt_type} placed — {opt_sym} × {qty} "
                        f"@ ₹{fill:.2f}  |  SL ₹{sl:.1f}  Target ₹{tgt:.1f}"
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Order failed: {e}")

            st.markdown("</div>", unsafe_allow_html=True)

        else:
            # ── POSITION card ─────────────────────────────────────────────────
            st.markdown(
                "<div style='border:1px solid #e0e0e0;border-radius:10px;"
                "padding:14px 16px;background:#fff;margin-bottom:6px'>"
                "<div style='font-size:12px;font-weight:700;color:#555;"
                "text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px'>"
                "Open Position</div>",
                unsafe_allow_html=True,
            )
            sym          = pos["tradingsymbol"]
            exchange     = pos["exchange"]
            opt_type     = pos["type"]
            rem_qty      = pos["remaining_qty"]
            entry        = pos["entry_price"]
            sl           = pos["sl"]
            tgt          = pos["target"]
            trail_lvl    = pos["trail_level"]
            tgt_lvl      = pos["target_level"]
            sl_oid       = pos.get("sl_order_id", "")

            # live LTP for the position's option
            pos_ltp = opt_q.get(f"{exchange}:{sym}", entry)
            pnl     = round((pos_ltp - entry) * rem_qty, 2)
            pnl_pct = (pos_ltp - entry) / entry * 100 if entry else 0
            pnl_cls = "pnl-pos" if pnl >= 0 else "pnl-neg"
            pnl_sign= "+" if pnl >= 0 else ""
            sl_pct_now = (pos_ltp - sl) / sl * 100 if sl else 0

            # visual alert if SL breached or target hit
            if pos_ltp <= sl and sl > 0:
                st.error(f"SL HIT! {sym} LTP ₹{pos_ltp:.2f} ≤ SL ₹{sl:.2f} — "
                         f"SL-M order should auto-execute. Click SELL if still open.")
            elif pos_ltp >= tgt and tgt > 0:
                st.success(f"TARGET HIT! {sym} LTP ₹{pos_ltp:.2f} ≥ Target ₹{tgt:.2f} — "
                           f"click SELL to book profit.")

            opt_col = "#1b5e20" if opt_type == "CE" else "#b71c1c"
            st.markdown(
                f"<div class='pos-card' style='border-color:{opt_col}'>"
                f"<div style='font-size:14px;font-weight:700;color:{opt_col}'>"
                f"{sym} &nbsp;|&nbsp; {rem_qty} qty ({pos['lots']} lots)</div>"
                f"<div style='font-size:12px;color:#555;margin:4px 0'>"
                f"Entry &#8377;{entry:.2f} &nbsp; LTP &#8377;{pos_ltp:.2f}</div>"
                f"<div class='{pnl_cls}'>{pnl_sign}&#8377;{abs(pnl):,.2f} "
                f"({pnl_sign}{pnl_pct:.2f}%)</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

            # SL / Target display
            sl_col = "#b71c1c" if pos_ltp > sl * 1.02 else "#ff5722"
            tg_col = "#1b5e20" if pos_ltp < tgt * 0.98 else "#2e7d32"
            st.markdown(
                f"<div class='sl-line'>"
                f"SL &nbsp;<b style='color:{sl_col}'>&#8377;{sl:.1f}</b>"
                f"<span style='color:#aaa;font-size:11px'> "
                f"(-{SL_DEFAULT_PCT:.0f}% / Trail {trail_lvl})</span>"
                f" &nbsp;&nbsp; "
                f"Target &nbsp;<b style='color:{tg_col}'>&#8377;{tgt:.1f}</b>"
                f"<span style='color:#aaa;font-size:11px'> "
                f"(+{TARGET_LEVELS[tgt_lvl]:.0f}%)</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

            # action buttons row 1
            a1, a2, a3 = st.columns(3)
            with a1:
                if st.button("Trail SL ▶", key=f"trail_{idx_name}", use_container_width=True):
                    next_trail = min(trail_lvl + 1, len(TRAIL_LEVELS) - 1)
                    new_sl = round(entry * (1 - TRAIL_LEVELS[next_trail] / 100), 1)
                    _modify_slm(kite, sl_oid, new_sl, paper)
                    pos["sl"]          = new_sl
                    pos["trail_level"] = next_trail
                    _set_pos(idx_name, pos)
                    st.toast(f"SL moved to ₹{new_sl:.1f} (trail {next_trail})")
                    st.rerun()

            with a2:
                if st.button("Target Fast ▶", key=f"tgtfast_{idx_name}", use_container_width=True):
                    next_tgt_lvl = min(tgt_lvl + 1, len(TARGET_LEVELS) - 1)
                    new_tgt = round(entry * (1 + TARGET_LEVELS[next_tgt_lvl] / 100), 1)
                    pos["target"]       = new_tgt
                    pos["target_level"] = next_tgt_lvl
                    _set_pos(idx_name, pos)
                    st.toast(f"Target moved to ₹{new_tgt:.1f} (+{TARGET_LEVELS[next_tgt_lvl]}%)")
                    st.rerun()

            with a3:
                half_qty = rem_qty // 2
                partial_ok = half_qty >= 1 and not pos.get("partial_done")
                if st.button(
                    f"Partial 50% ({half_qty})",
                    key=f"partial_{idx_name}",
                    use_container_width=True,
                    disabled=not partial_ok,
                ):
                    try:
                        # cancel old SL, sell half, new SL for remainder
                        _cancel_order(kite, sl_oid, paper)
                        _place_market_order(kite, exchange, sym, "SELL", half_qty, paper)
                        new_rem = rem_qty - half_qty
                        new_sl_oid = _place_slm_order(kite, exchange, sym, new_rem, sl, paper)
                        pos["remaining_qty"] = new_rem
                        pos["sl_order_id"]   = new_sl_oid
                        pos["partial_done"]  = True
                        _set_pos(idx_name, pos)
                        st.toast(f"Partial booked {half_qty} qty, {new_rem} remaining.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Partial failed: {e}")

            # action buttons row 2
            b1, b2 = st.columns(2)
            with b1:
                if st.button(
                    "Delink (no sell)",
                    key=f"delink_{idx_name}",
                    use_container_width=True,
                ):
                    _cancel_order(kite, sl_oid, paper)
                    _clear_pos(idx_name)
                    st.toast(f"{idx_name}: tracking removed — no sell order placed.")
                    st.rerun()

            with b2:
                if st.button(
                    f"SELL ALL ({rem_qty})",
                    key=f"sell_{idx_name}",
                    type="primary",
                    use_container_width=True,
                ):
                    # verify position still open before selling
                    live_qty = _get_open_qty(kite, sym, paper)
                    if live_qty == 0:
                        _cancel_order(kite, sl_oid, paper)
                        _clear_pos(idx_name)
                        st.warning("Position already closed. Delinked.")
                        st.rerun()
                    else:
                        try:
                            _cancel_order(kite, sl_oid, paper)
                            _place_market_order(kite, exchange, sym, "SELL", rem_qty, paper)
                            _clear_pos(idx_name)
                            mode_str = "PAPER" if paper else "LIVE"
                            st.success(f"{mode_str} SELL {sym} × {rem_qty} placed.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Sell failed: {e}")

            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
