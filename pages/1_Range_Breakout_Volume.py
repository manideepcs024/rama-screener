"""
ChartInk-style Range Breakout With Volume Screener
Conditions (mirrors: chartink.com/screener/range-break-out-with-volume-min-3):
  1. Close > Highest(High, 20 days)  — price breaks above 20-day range
  2. Volume >= 3× 20-day average volume
  3. % change >= 3%

Run alongside rama_screener.py:
    streamlit run rama_screener.py   (this page appears in the sidebar nav)
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests as _req
import streamlit as st
import yfinance as yf
from dotenv import load_dotenv
from kiteconnect import KiteConnect

_log = logging.getLogger("range_breakout")
logging.basicConfig(level=logging.INFO)

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH, override=True)

NSE_N500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_EQ_URL   = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_FNO_URL  = "https://archives.nseindia.com/content/fo/fo_mktlots.csv"
MAX_WORKERS  = 30

# ── Telegram ──────────────────────────────────────────────────────────────────
def _tg_creds() -> tuple[str, str]:
    vals: dict[str, str] = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    vals[k.strip()] = v.strip()
    except Exception:
        pass
    tok = vals.get("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    cid = vals.get("TELEGRAM_CHAT_ID",   os.getenv("TELEGRAM_CHAT_ID",   "")).strip()
    return tok, cid

def _tg_enabled() -> bool:
    tok, cid = _tg_creds()
    return bool(tok and cid)

def send_telegram(text: str) -> bool:
    tok, cid_raw = _tg_creds()
    if not tok or not cid_raw:
        return False
    chat_ids = [c.strip() for c in cid_raw.split(",") if c.strip()]
    url      = f"https://api.telegram.org/bot{tok}/sendMessage"
    any_ok   = False
    for cid in chat_ids:
        try:
            r = _req.post(url, json={"chat_id": cid, "text": text, "parse_mode": "Markdown"}, timeout=5)
            if r.ok:
                any_ok = True
        except Exception as e:
            _log.warning(f"Telegram error {cid}: {e}")
    return any_ok

# ── Kite ──────────────────────────────────────────────────────────────────────
def _kite_connect() -> KiteConnect:
    kite = KiteConnect(api_key=os.getenv("ZERODHA_API_KEY", ""))
    kite.set_access_token(os.getenv("ZERODHA_ACCESS_TOKEN", ""))
    return kite

def get_kite() -> KiteConnect:
    now = time.time()
    if "rb_kite" not in st.session_state or (now - st.session_state.get("rb_kite_ts", 0)) > 1800:
        try:
            if "rb_kite" in st.session_state:
                st.session_state["rb_kite"].profile()
                st.session_state["rb_kite_ts"] = now
            else:
                raise Exception
        except Exception:
            st.session_state["rb_kite"] = _kite_connect()
            st.session_state["rb_kite_ts"] = now
    return st.session_state["rb_kite"]

# ── Universe ──────────────────────────────────────────────────────────────────
@st.cache_data(ttl=86400, show_spinner=False)
def load_nifty500() -> list[str]:
    try:
        df = pd.read_csv(NSE_N500_URL)
        col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        return sorted(df[col].dropna().str.strip().str.upper().tolist())
    except Exception:
        return []

@st.cache_data(ttl=86400, show_spinner=False)
def load_all_nse() -> list[str]:
    try:
        df = pd.read_csv(NSE_EQ_URL)
        col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        return sorted(df[col].dropna().str.strip().str.upper().tolist())
    except Exception:
        return []

@st.cache_data(ttl=86400, show_spinner=False)
def load_fno_stocks() -> list[str]:
    try:
        kite = get_kite()
        inst = kite.instruments("NFO")
        _index = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
        syms = sorted({
            i["name"].strip().upper()
            for i in inst
            if i.get("instrument_type") == "FUT"
            and i.get("name", "").strip().upper() not in _index
        })
        if len(syms) > 50:
            return syms
    except Exception:
        pass
    # hardcoded fallback
    return sorted([
        "AARTIIND","ABB","ABBOTINDIA","ABCAPITAL","ABFRL","ACC","ADANIENT",
        "ADANIPORTS","ALKEM","AMBUJACEM","APOLLOHOSP","APOLLOTYRE","ASHOKLEY",
        "ASIANPAINT","ASTRAL","ATUL","AUBANK","AUROPHARMA","AXISBANK","BAJAJ-AUTO",
        "BAJAJFINSV","BAJFINANCE","BALKRISIND","BANDHANBNK","BANKBARODA",
        "BATAINDIA","BEL","BERGEPAINT","BHARTIARTL","BHEL","BIOCON","BOSCHLTD",
        "BPCL","BRITANNIA","BSOFT","CANBK","CANFINHOME","CHAMBLFERT","CHOLAFIN",
        "CIPLA","COALINDIA","COFORGE","COLPAL","CONCOR","COROMANDEL","CROMPTON",
        "CUMMINSIND","DABUR","DALBHARAT","DEEPAKNTR","DIVISLAB","DIXON","DLF",
        "DRREDDY","EICHERMOT","ESCORTS","EXIDEIND","FEDERALBNK","FORTIS","GAIL",
        "GLENMARK","GMRINFRA","GODREJCP","GODREJPROP","GRANULES","GRASIM",
        "GUJARATGAS","HAL","HAVELLS","HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO",
        "HINDALCO","HINDCOPPER","HINDPETRO","HINDUNILVR","ICICIBANK","ICICIGI",
        "ICICIPRULI","IDFCFIRSTB","IEX","IGL","INDHOTEL","INDIAMART","INDUSINDBK",
        "INDUSTOWER","INFY","IOC","IPCALAB","IRCTC","ITC","JINDALSTEL","JSWSTEEL",
        "JUBLFOOD","KOTAKBANK","L&TFH","LALPATHLAB","LAURUSLABS","LICHSGFIN","LT",
        "LTIM","LTTS","LUPIN","M&M","M&MFIN","MANAPPURAM","MARICO","MARUTI","MCX",
        "MFSL","MGL","MOTHERSON","MPHASIS","MRF","MUTHOOTFIN","NAUKRI","NESTLEIND",
        "NMDC","NTPC","OBEROIRLTY","OFSS","ONGC","PAGEIND","PERSISTENT","PETRONET",
        "PFC","PIDILITIND","PIIND","PNB","POLYCAB","POWERGRID","PVRINOX","RBLBANK",
        "RECLTD","RELIANCE","SAIL","SBICARD","SBILIFE","SBIN","SHREECEM","SIEMENS",
        "SRF","SUNPHARMA","SUNTV","SYNGENE","TATACHEM","TATACOMM","TATACONSUM",
        "TATAMOTORS","TATAPOWER","TATASTEEL","TCS","TECHM","TITAN","TORNTPHARM",
        "TRENT","TVSMOTOR","UBL","ULTRACEMCO","UPL","VEDL","VOLTAS","WIPRO",
        "ZEEL","ZOMATO",
    ])

# ── Historical data (20-day High + avg volume) ───────────────────────────────
def _fetch_hist(sym: str) -> dict | None:
    try:
        tk  = yf.Ticker(sym + ".NS")
        df  = tk.history(period="25d", interval="1d", auto_adjust=True)
        if df is None or len(df) < 6:
            return None
        close_today = float(df["Close"].iloc[-1])
        high_prev   = float(df["High"].iloc[:-1].max())          # 20-day high excluding today
        avg_vol     = float(df["Volume"].iloc[:-1].mean())        # avg volume excluding today
        vol_today   = float(df["Volume"].iloc[-1])
        pct_chg     = (close_today - float(df["Close"].iloc[-2])) / float(df["Close"].iloc[-2]) * 100
        range_bo    = close_today >= high_prev
        vol_ratio   = vol_today / avg_vol if avg_vol > 0 else 0.0
        return {
            "symbol":     sym,
            "close":      round(close_today, 2),
            "high_prev":  round(high_prev, 2),
            "avg_vol":    round(avg_vol),
            "vol_today":  round(vol_today),
            "vol_ratio":  round(vol_ratio, 2),
            "pct_chg":    round(pct_chg, 2),
            "range_bo":   range_bo,
        }
    except Exception:
        return None

@st.cache_data(ttl=300, show_spinner=False)
def load_hist_batch(universe: tuple) -> pd.DataFrame:
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for r in ex.map(_fetch_hist, universe):
            if r:
                rows.append(r)
    return pd.DataFrame(rows) if rows else pd.DataFrame()

# ── Live quotes via Kite ──────────────────────────────────────────────────────
@st.cache_data(ttl=30, show_spinner=False)
def fetch_quotes(syms: tuple, _kite_ts: float) -> dict:
    try:
        kite = get_kite()
        inst = [f"NSE:{s}" for s in syms]
        raw  = kite.quote(inst)
        out  = {}
        for s in syms:
            q = raw.get(f"NSE:{s}", {})
            out[s] = {
                "ltp":        q.get("last_price", 0.0),
                "pct":        q.get("net_change", 0.0),
                "volume":     q.get("volume", 0),
                "ohlc":       q.get("ohlc", {}),
            }
        return out
    except Exception:
        return {}

# ── Apply ChartInk conditions ─────────────────────────────────────────────────
def apply_conditions(df: pd.DataFrame,
                     min_pct: float,
                     min_vol_ratio: float,
                     require_range_bo: bool) -> pd.DataFrame:
    if df.empty:
        return df
    if require_range_bo:
        df = df[df["range_bo"] == True]
    df = df[df["vol_ratio"] >= min_vol_ratio]
    df = df[df["pct_chg"]   >= min_pct]
    return df.sort_values("vol_ratio", ascending=False).reset_index(drop=True)

# ── Telegram signal ───────────────────────────────────────────────────────────
def send_rb_signal(row: pd.Series) -> None:
    sym  = row["symbol"]
    cmp  = row.get("ltp", row["close"])
    pct  = row.get("live_pct", row["pct_chg"])
    vol  = row["vol_ratio"]
    prev = row["high_prev"]
    msg  = (
        f"🚀 *RANGE BREAKOUT — {sym}*\n"
        f"{'─'*28}\n"
        f"📍 CMP:         ₹{cmp:,.2f}  _({pct:+.1f}%)_\n"
        f"📊 20D High:    ₹{prev:,.2f}  ✅ Broken\n"
        f"📦 Vol Ratio:   {vol:.1f}×\n"
        f"{'─'*28}\n"
        f"⏰  {datetime.now():%d %b %Y %H:%M}"
    )
    send_telegram(msg)

# ── Page ──────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Range Breakout Screener", page_icon="🚀", layout="wide")
st.title("🚀 Range Breakout + Volume Screener")
st.caption("Mirrors ChartInk: Close > 20D High  |  Volume ≥ 3×  |  % Change ≥ 3%")

# ── Sidebar controls ──────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")
    universe_choice = st.selectbox("Universe", ["Nifty F&O", "Nifty 500", "All NSE"],
                                   help="F&O = ~180 most liquid stocks with futures/options")
    st.markdown("---")
    st.subheader("ChartInk Conditions")
    require_bo  = st.toggle("Close > 20D High (Range Breakout)", value=True)
    min_vol     = st.slider("Min Volume Ratio (×)", 1.0, 10.0, 3.0, 0.5)
    min_pct     = st.slider("Min % Change", 0.0, 10.0, 3.0, 0.5)
    st.markdown("---")
    st.subheader("Telegram")
    if _tg_enabled():
        _, _cid_raw = _tg_creds()
        _n = len([c for c in _cid_raw.split(",") if c.strip()])
        st.success(f"✅ Connected ({_n} recipient{'s' if _n != 1 else ''})")
    else:
        st.warning("⚠️ Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env")
    if st.button("🔔 Test Telegram", key="rb_tg_test"):
        ok = send_telegram("✅ *Range Breakout Screener* connected!")
        st.toast("Telegram OK ✅" if ok else "Telegram failed ❌")
    st.markdown("---")
    run_btn = st.button("🔍 Scan Now", type="primary", use_container_width=True)

# ── Load data ──────────────────────────────────────────────────────────────────
if universe_choice == "Nifty F&O":
    universe = load_fno_stocks()
elif universe_choice == "Nifty 500":
    universe = load_nifty500()
else:
    universe = load_all_nse()

if not universe:
    st.error("Could not load stock universe. Check internet connection.")
    st.stop()

# Auto-run on page load OR when button pressed
if "rb_last_scan" not in st.session_state or run_btn:
    st.session_state["rb_last_scan"] = time.time()

with st.spinner(f"Loading historical data for {len(universe)} stocks…"):
    hist_df = load_hist_batch(tuple(universe))

if hist_df.empty:
    st.warning("No historical data loaded.")
    st.stop()

# Apply conditions
result = apply_conditions(hist_df, min_pct, min_vol, require_bo)

# ── Live LTP via Kite ─────────────────────────────────────────────────────────
if not result.empty:
    try:
        kite   = get_kite()
        quotes = fetch_quotes(tuple(result["symbol"].tolist()), st.session_state["rb_last_scan"])
        result["ltp"]      = result["symbol"].map(lambda s: quotes.get(s, {}).get("ltp", result.loc[result["symbol"]==s, "close"].values[0]))
        result["live_pct"] = result["symbol"].map(lambda s: quotes.get(s, {}).get("pct", result.loc[result["symbol"]==s, "pct_chg"].values[0]))
    except Exception:
        result["ltp"]      = result["close"]
        result["live_pct"] = result["pct_chg"]

# ── Summary bar ──────────────────────────────────────────────────────────────
col1, col2, col3, col4 = st.columns(4)
col1.metric("Stocks Scanned",  len(hist_df))
col2.metric("Breakouts Found", len(result), delta=f"+{len(result)}" if len(result) else None)
col3.metric("Universe",        universe_choice)
col4.metric("Last Scan",       datetime.now().strftime("%H:%M:%S"))

st.markdown("---")

if result.empty:
    st.info("No stocks match the current conditions. Try lowering the thresholds.")
    st.stop()

# ── Results table ─────────────────────────────────────────────────────────────
st.subheader(f"✅ {len(result)} Breakout{'s' if len(result) != 1 else ''} Found")

HEADERS = [
    ("#",          0.3),
    ("Symbol",     1.2),
    ("LTP ₹",      1.0),
    ("Chg %",      0.8),
    ("Vol Ratio",  0.9),
    ("Vol Today",  1.0),
    ("20D High ₹", 1.0),
    ("Breakout",   0.8),
    ("Action",     1.2),
]
hcols = st.columns([w for _, w in HEADERS])
for col, (label, _) in zip(hcols, HEADERS):
    col.markdown(f"**{label}**")
st.markdown("<hr style='margin:4px 0'>", unsafe_allow_html=True)

for idx, row in result.iterrows():
    sym    = row["symbol"]
    ltp    = row.get("ltp", row["close"])
    pct    = row.get("live_pct", row["pct_chg"])
    vol_r  = row["vol_ratio"]
    vol_t  = int(row["vol_today"])
    h20    = row["high_prev"]
    bo     = row["range_bo"]

    pct_color = "#1b5e20" if pct >= 0 else "#b71c1c"
    bo_badge  = "<span style='color:#1b5e20;font-weight:700'>✅ YES</span>" if bo else "<span style='color:#b71c1c'>❌ NO</span>"

    rcols = st.columns([w for _, w in HEADERS])
    rcols[0].write(idx + 1)
    rcols[1].markdown(f"**{sym}**")
    rcols[2].write(f"₹{ltp:,.2f}")
    rcols[3].markdown(f"<span style='color:{pct_color}'>{pct:+.2f}%</span>", unsafe_allow_html=True)
    rcols[4].write(f"{vol_r:.1f}×")
    rcols[5].write(f"{vol_t:,}")
    rcols[6].write(f"₹{h20:,.2f}")
    rcols[7].markdown(bo_badge, unsafe_allow_html=True)

    with rcols[8]:
        bc, tgc = st.columns(2)
        with bc:
            if st.button("BUY", key=f"rb_buy_{sym}", type="primary"):
                st.toast(f"BUY signal: {sym} @ ₹{ltp:,.2f}")
        with tgc:
            if st.button("📨", key=f"rb_tg_{sym}", disabled=not _tg_enabled(),
                         help="Send to Telegram"):
                send_rb_signal(row)
                st.toast(f"📨 Sent {sym}")

st.markdown("---")

# ── Auto Telegram: top 5 by vol ratio ────────────────────────────────────────
def _auto_top5() -> None:
    if not _tg_enabled() or result.empty:
        return
    top5     = result.nlargest(5, "vol_ratio").reset_index(drop=True)
    _now     = time.time()
    _last_ts = st.session_state.get("rb_tg_ts", 0)
    _last    = st.session_state.get("rb_tg_syms", [])
    _cur     = top5["symbol"].tolist()
    if _now - _last_ts < 300 and _cur == _last:
        return
    st.session_state["rb_tg_ts"]   = _now
    st.session_state["rb_tg_syms"] = _cur
    lines = [
        "🚀 *TOP RANGE BREAKOUTS*",
        f"_{datetime.now():%d %b %Y %H:%M}_",
        "─" * 28,
    ]
    for i, row in top5.iterrows():
        sym  = row["symbol"]
        ltp  = row.get("ltp", row["close"])
        pct  = row.get("live_pct", row["pct_chg"])
        vol  = row["vol_ratio"]
        h20  = row["high_prev"]
        lines.append(
            f"{i+1}\\. *{sym}*\n"
            f"   CMP ₹{ltp:,.2f}  |  Chg {pct:+.1f}%  |  Vol {vol:.1f}×\n"
            f"   20D High ₹{h20:,.2f}  ✅ Broken"
        )
    send_telegram("\n".join(lines))

_auto_top5()

st.caption(f"Data: yfinance (historical, 25D) + Kite (live LTP)  ·  Auto-refreshes on scan")
