"""
Free Screener — backup screener using Yahoo Finance (free, ~15 min delay).
No Kite/Zerodha API needed. No buy/sell buttons — analysis only.
"""
from __future__ import annotations

import concurrent.futures
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

st.set_page_config(
    page_title="Free Screener — RAMA",
    page_icon="📊",
    layout="wide",
)

from _auth import require_login
require_login()

_log = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────
MAX_WORKERS     = 20
REFRESH_SECONDS = 120   # 2-min refresh (yfinance has ~15 min delay anyway)
_DEF_MIN_PCT    = 1.5
_DEF_VOL_SURGE  = 1.5
_DEF_MARKET_CAP = 5_000
_DEF_PE_MAX     = 80
_DEF_EPS_MIN    = 0.0
_DEF_INST_MIN   = 0.0

NSE_FNO_URL = "https://archives.nseindia.com/content/fo/fo_mktlots.csv"
NSE_N500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

st.markdown("""
<style>
#flonnect-my-app { display: none !important; }
div[data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
div[data-testid="stMarkdownContainer"] > p { margin-bottom: 0 !important; }
.free-badge {
    display:inline-block; background:#e8f5e9; color:#1b5e20;
    font-size:11px; font-weight:700; padding:2px 8px; border-radius:5px;
    border:1px solid #a5d6a7; margin-left:8px; vertical-align:middle;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<h1 style='text-align:center;color:#1a73e8;letter-spacing:3px;font-family:monospace'>
📊 &nbsp; FREE SCREENER <span class='free-badge'>Yahoo Finance</span>
</h1>
<p style='text-align:center;color:#888;margin-top:0;font-size:13px'>
Backup screener · No Zerodha API needed · ~15 min delayed prices
</p>
<hr style='border:1px solid #eee;margin-bottom:4px'>
""", unsafe_allow_html=True)

# ── universe loaders ──────────────────────────────────────────────────────────

@st.cache_data(ttl=86_400, show_spinner="Fetching F&O universe…")
def load_fno_stocks() -> list[str]:
    try:
        df = pd.read_csv(NSE_FNO_URL)
        col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        syms = df[col].dropna().astype(str).str.strip().tolist()
        return [s for s in syms if s and s != "Symbol"][:220]
    except Exception:
        return _FALLBACK_FNO


@st.cache_data(ttl=86_400, show_spinner="Fetching Nifty 500…")
def load_nifty500() -> list[str]:
    try:
        df = pd.read_csv(NSE_N500_URL)
        col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        return df[col].dropna().astype(str).str.strip().tolist()
    except Exception:
        return load_fno_stocks()


_FALLBACK_FNO = [
    "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","HINDUNILVR","SBIN","BHARTIARTL",
    "AXISBANK","KOTAKBANK","LT","WIPRO","HCLTECH","SUNPHARMA","BAJFINANCE","MARUTI",
    "NTPC","POWERGRID","ONGC","COALINDIA","ADANIENT","ADANIPORTS","TATAMOTORS",
    "TATASTEEL","JSWSTEEL","HINDALCO","ULTRACEMCO","GRASIM","ASIANPAINT","NESTLEIND",
    "TITAN","BAJAJFINSV","TECHM","M&M","DRREDDY","CIPLA","DIVISLAB","APOLLOHOSP",
    "EICHERMOT","HEROMOTOCO","BPCL","IOC","TATACONSUM","BRITANNIA","PIDILITIND",
    "DABUR","MARICO","GODREJCP","BERGEPAINT","HAVELLS","VOLTAS","WHIRLPOOL",
    "MUTHOOTFIN","CHOLAFIN","LTIM","PERSISTENT","COFORGE","MPHASIS","OFSS",
]


def load_universe(choice: str) -> list[str]:
    if choice == "Nifty F&O":
        return load_fno_stocks()
    elif choice == "Nifty 500":
        return load_nifty500()
    else:
        return load_fno_stocks()


# ── daily data (RSI, EMA, volume baseline, patterns) ─────────────────────────

def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, float("nan"))
    rsi   = 100 - 100 / (1 + rs)
    return round(float(rsi.iloc[-1]), 1) if not rsi.empty else 50.0


def _calc_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    try:
        h, l, c = high.values, low.values, close.values
        n = len(c)
        if n < period + 2:
            return 20.0
        tr_arr, pdm_arr, ndm_arr = [], [], []
        for i in range(1, n):
            tr  = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
            pdm = max(h[i] - h[i-1], 0) if (h[i] - h[i-1]) > (l[i-1] - l[i]) else 0
            ndm = max(l[i-1] - l[i], 0) if (l[i-1] - l[i]) > (h[i] - h[i-1]) else 0
            tr_arr.append(tr); pdm_arr.append(pdm); ndm_arr.append(ndm)
        tr_s   = pd.Series(tr_arr).ewm(span=period, adjust=False).mean()
        pdm_s  = pd.Series(pdm_arr).ewm(span=period, adjust=False).mean()
        ndm_s  = pd.Series(ndm_arr).ewm(span=period, adjust=False).mean()
        pdi    = 100 * pdm_s / tr_s.replace(0, float("nan"))
        ndi    = 100 * ndm_s / tr_s.replace(0, float("nan"))
        dx     = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, float("nan"))
        adx    = dx.ewm(span=period, adjust=False).mean()
        return round(float(adx.iloc[-1]), 1)
    except Exception:
        return 20.0


def _bb_squeeze(closes: pd.Series, period: int = 20, threshold: float = 0.70) -> bool:
    if len(closes) < period:
        return False
    rolling = closes.rolling(period)
    std = rolling.std().iloc[-1]
    mean = rolling.mean().iloc[-1]
    bw = (std / mean * 100) if mean else 0
    return bw < threshold * 2


def _nr7(highs: pd.Series, lows: pd.Series) -> bool:
    if len(highs) < 7:
        return False
    ranges = (highs - lows).tail(7)
    return float(ranges.iloc[-1]) == float(ranges.min())


def _parse_sym_daily(sym: str, raw: object, single: bool) -> tuple[str, dict]:
    """Extract daily indicators for one symbol from a batch yf.download result."""
    try:
        sym_yf = sym + ".NS"
        closes = (raw["Close"]  if single else raw["Close"][sym_yf]).dropna()
        highs  = (raw["High"]   if single else raw["High"][sym_yf]).dropna()
        lows   = (raw["Low"]    if single else raw["Low"][sym_yf]).dropna()
        opens  = (raw["Open"]   if single else raw["Open"][sym_yf]).dropna()
        vols   = (raw["Volume"] if single else raw["Volume"][sym_yf]).dropna()
        if len(closes) < 20:
            return sym, {}
        rsi     = _calc_rsi(closes)
        adx     = _calc_adx(highs, lows, closes)
        avg_vol = float(vols.tail(20).mean()) if len(vols) >= 20 else float(vols.mean())
        ema21   = float(closes.ewm(span=21,  adjust=False).mean().iloc[-1])
        ema50   = float(closes.ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200  = float(closes.ewm(span=200, adjust=False).mean().iloc[-1])
        ltp     = float(closes.iloc[-1])
        h52w    = float(highs.tail(252).max())
        l52w    = float(lows.tail(252).min())
        nr7_flag   = _nr7(highs, lows)
        bb_sq      = _bb_squeeze(closes)
        inside_bar = (len(closes) >= 2 and
                      float(highs.iloc[-1]) <= float(highs.iloc[-2]) and
                      float(lows.iloc[-1])  >= float(lows.iloc[-2]))
        range_bo   = (len(closes) >= 6 and
                      float(closes.iloc[-1]) > float(highs.iloc[-6:-1].max()))
        if len(closes) >= 4:
            r   = [(float(highs.iloc[i]) - float(lows.iloc[i])) for i in range(-4, 0)]
            vcp = r[-1] < r[-2] < r[-3]
        else:
            vcp = False
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd_line    = ema12 - ema26
        signal_line  = macd_line.ewm(span=9, adjust=False).mean()
        macd_bullish = float(macd_line.iloc[-1]) > float(signal_line.iloc[-1])
        last10_cl = closes.tail(11)
        last10_op = opens.reindex(last10_cl.index)
        up_days   = int(sum(1 for c, o in zip(last10_cl, last10_op) if c > o))
        prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else float(closes.iloc[-1])
        return sym, {
            "avg_vol": avg_vol, "rsi": rsi, "adx": adx,
            "ema21": ema21, "ema50": ema50, "ema200": ema200,
            "above_ema21": ltp > ema21, "above_ema50": ltp > ema50, "above_ema200": ltp > ema200,
            "high_52w_yf": h52w, "low_52w_yf": l52w,
            "prev_close": prev_close,
            "bb_squeeze": bb_sq, "nr7": nr7_flag,
            "inside_bar": inside_bar, "range_bo": range_bo, "vcp": vcp,
            "macd_bullish": macd_bullish,
            "up_days": up_days, "buy_days": up_days, "sell_days": 10 - up_days,
        }
    except Exception:
        return sym, {}


@st.cache_data(ttl=86_400, show_spinner=False)
def load_daily_data(symbols: tuple) -> dict[str, dict]:
    """Batch-download 1 year of daily OHLCV for all symbols in one yf.download call."""
    if not symbols:
        return {}
    result: dict[str, dict] = {}
    chunk_size = 100   # yfinance handles ~100 symbols comfortably per call
    sym_list   = list(symbols)
    for i in range(0, len(sym_list), chunk_size):
        chunk     = sym_list[i:i + chunk_size]
        yf_syms   = [s + ".NS" for s in chunk]
        single    = len(chunk) == 1
        try:
            raw = yf.download(
                yf_syms, period="1y", interval="1d",
                auto_adjust=True, progress=False, threads=True,
            )
            if raw is None or raw.empty:
                continue
            for sym in chunk:
                sym_key, data = _parse_sym_daily(sym, raw, single)
                if data:
                    result[sym_key] = data
        except Exception as e:
            _log.warning(f"Daily batch download chunk {i}: {e}")
    return result


# ── live quotes via yfinance daily bar (fast — one bar per stock) ─────────────

@st.cache_data(ttl=120, show_spinner=False)   # 2-min cache
def load_yf_quotes(symbols: tuple) -> dict[str, dict]:
    """
    Use period='2d' interval='1d' — one API call gets today's OHLCV bar
    AND yesterday's close in a single tiny response (~10× faster than 1-min).
    """
    if not symbols:
        return {}
    yf_syms = [s + ".NS" for s in symbols]
    result: dict[str, dict] = {}
    chunk_size = 200   # daily bars are tiny — can batch more per call
    sym_list   = list(symbols)
    for i in range(0, len(yf_syms), chunk_size):
        batch_yf  = yf_syms[i:i + chunk_size]
        batch_sym = sym_list[i:i + chunk_size]
        try:
            raw = yf.download(
                batch_yf, period="2d", interval="1d",
                auto_adjust=True, progress=False, threads=True,
            )
            if raw is None or raw.empty:
                continue
            single = len(batch_yf) == 1
            for sym, sym_yf in zip(batch_sym, batch_yf):
                try:
                    close_s = (raw["Close"]  if single else raw["Close"][sym_yf]).dropna()
                    high_s  = (raw["High"]   if single else raw["High"][sym_yf]).dropna()
                    low_s   = (raw["Low"]    if single else raw["Low"][sym_yf]).dropna()
                    open_s  = (raw["Open"]   if single else raw["Open"][sym_yf]).dropna()
                    vol_s   = (raw["Volume"] if single else raw["Volume"][sym_yf]).dropna()
                    if close_s.empty:
                        continue
                    ltp      = float(close_s.iloc[-1])
                    day_open = float(open_s.iloc[-1])
                    day_high = float(high_s.iloc[-1])
                    day_low  = float(low_s.iloc[-1])
                    result[sym] = {
                        "last_price": ltp,
                        "volume": int(vol_s.iloc[-1]) if not vol_s.empty else 0,
                        "ohlc": {
                            "open":  day_open,
                            "high":  day_high,
                            "low":   day_low,
                            "close": day_open,
                        },
                        "week_52_high": 0, "week_52_low": 0,
                        "upper_circuit_limit": 0, "lower_circuit_limit": 0,
                    }
                except Exception:
                    continue
        except Exception as e:
            _log.warning(f"yf quote chunk {i}: {e}")
    return result


# ── 15-min VWAP ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def load_15min_vwap(symbols: tuple) -> dict[str, dict]:
    if not symbols:
        return {}
    yf_syms = [s + ".NS" for s in symbols]
    result  = {}
    _default = {"vwap_15": 0.0, "above_vwap_15": None}
    try:
        raw = yf.download(yf_syms, period="1d", interval="15m",
                          auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            return {s: _default for s in symbols}
        single = len(yf_syms) == 1
        for sym, sym_yf in zip(symbols, yf_syms):
            try:
                close  = (raw["Close"]  if single else raw["Close"][sym_yf]).dropna()
                high   = (raw["High"]   if single else raw["High"][sym_yf]).dropna()
                low    = (raw["Low"]    if single else raw["Low"][sym_yf]).dropna()
                volume = (raw["Volume"] if single else raw["Volume"][sym_yf]).dropna()
                if len(close) < 3:
                    result[sym] = _default; continue
                tp      = (high + low + close) / 3
                cvol    = volume.cumsum()
                lc      = float(cvol.iloc[-1])
                vwap_15 = round(float((tp * volume).cumsum().iloc[-1] / lc), 2) if lc > 0 else 0.0
                ltp     = float(close.iloc[-1])
                result[sym] = {
                    "vwap_15": vwap_15,
                    "above_vwap_15": ltp >= vwap_15 if vwap_15 > 0 else None,
                }
            except Exception:
                result[sym] = _default
    except Exception:
        return {s: _default for s in symbols}
    return result


# ── news ──────────────────────────────────────────────────────────────────────

def _fetch_news_one(sym: str) -> tuple[str, dict]:
    """Lightweight news fetch via GNews (free, no API key needed)."""
    try:
        import requests
        from textblob import TextBlob
        url = (
            f"https://gnews.io/api/v4/search?q={sym}+NSE+stock"
            f"&lang=en&max=5&token=free"
        )
        # Use Google News RSS as a free fallback
        rss_url = f"https://news.google.com/rss/search?q={sym}+NSE+stock&hl=en-IN&gl=IN&ceid=IN:en"
        resp = requests.get(rss_url, timeout=5,
                            headers={"User-Agent": "Mozilla/5.0"})
        headlines = []
        sentiment = 0.0
        if resp.ok:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "xml")
            items = soup.find_all("item")[:5]
            for item in items:
                title = item.find("title")
                if title:
                    h = title.text.strip()
                    headlines.append(h)
                    sentiment += TextBlob(h).sentiment.polarity
            if headlines:
                sentiment /= len(headlines)
        return sym, {
            "sentiment": round(sentiment, 3),
            "headlines": headlines[:3],
            "has_results": any(
                kw in " ".join(headlines).lower()
                for kw in ("results", "earnings", "profit", "revenue", "quarterly")
            ),
            "n_articles": len(headlines),
        }
    except Exception:
        return sym, {}


@st.cache_data(ttl=7_200, show_spinner=False)
def load_news_data(symbols: tuple, _v: int = 1) -> dict[str, dict]:
    result: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for sym, data in ex.map(_fetch_news_one, symbols):
            if data:
                result[sym] = data
    return result


# ── fundamentals ──────────────────────────────────────────────────────────────

def _fetch_fund(sym: str) -> dict | None:
    try:
        info = yf.Ticker(sym + ".NS").info
        mc = info.get("marketCap", 0) or 0
        return {
            "symbol": sym,
            "market_cap_cr": round(mc / 1e7, 0),
            "pe": info.get("trailingPE") or 0,
            "eps": info.get("trailingEps") or 0,
            "inst_pct": (info.get("heldPercentInstitutions") or 0) * 100,
            "name": info.get("shortName") or info.get("longName") or sym,
        }
    except Exception:
        return None


@st.cache_data(ttl=86_400, show_spinner=False)
def load_fundamentals(universe: tuple) -> pd.DataFrame:
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for r in ex.map(_fetch_fund, universe):
            if r:
                rows.append(r)
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["symbol", "market_cap_cr", "pe", "eps", "inst_pct", "name"])


def fundamental_filter(df: pd.DataFrame, enabled: bool,
                       mc_min: float, pe_max: float,
                       eps_min: float, inst_min: float) -> tuple[pd.DataFrame, dict]:
    if not enabled or df.empty:
        return df, {}
    out = df.copy()
    if mc_min > 0:
        out = out[out["market_cap_cr"] >= mc_min]
    if pe_max < 120:
        out = out[(out["pe"] > 0) & (out["pe"] <= pe_max)]
    if eps_min > 0:
        out = out[out["eps"] >= eps_min]
    if inst_min > 0:
        out = out[out["inst_pct"] >= inst_min]
    funnel = {
        "universe": len(df),
        "after_mc": len(out),
    }
    return out, funnel


# ── build intraday dataframe ───────────────────────────────────────────────────

def build_intraday_df(quotes: dict[str, dict],
                      daily_data: dict[str, dict],
                      news_data: dict[str, dict] | None = None,
                      vwap_15_data: dict[str, dict] | None = None) -> pd.DataFrame:
    rows = []
    for sym, q in quotes.items():
        try:
            ltp        = float(q["last_price"])
            ohlc       = q.get("ohlc", {})
            volume     = float(q.get("volume", 0))
            d          = daily_data.get(sym, {})
            # prev_close: from daily data (accurate yesterday close) or day open fallback
            prev_close = d.get("prev_close") or float(ohlc.get("close", 0))
            avg_vol    = d.get("avg_vol", 0)
            rsi        = d.get("rsi", 50.0)
            if prev_close <= 0 or ltp <= 0:
                continue
            pct_change   = (ltp - prev_close) / prev_close * 100
            vol_surge    = (volume / avg_vol) if avg_vol > 0 else 1.0
            high_52w     = d.get("high_52w_yf", ltp)
            low_52w      = d.get("low_52w_yf", ltp)
            if high_52w < low_52w:
                high_52w, low_52w = low_52w, high_52w
            dist_52w     = (high_52w - ltp) / ltp * 100 if high_52w > 0 else 0.0
            _uc          = prev_close * 1.20
            _lc          = prev_close * 0.80
            dist_to_uc   = (_uc - ltp) / ltp * 100 if ltp > 0 else 0.0
            dist_to_lc   = (ltp - _lc) / ltp * 100 if ltp > 0 else 0.0
            day_high     = float(ohlc.get("high", ltp))
            day_low      = float(ohlc.get("low", ltp))
            _vd          = (vwap_15_data or {}).get(sym, {})
            vwap_15      = float(_vd.get("vwap_15", 0.0) or 0.0)
            if vwap_15 > 0:
                vwap     = vwap_15
                above_vwap = ltp >= vwap
            else:
                vwap     = (float(ohlc.get("open", ltp)) + day_high + day_low + ltp) / 4
                above_vwap = ltp >= vwap
            near_day_high  = day_high > 0 and (day_high - ltp) / day_high * 100 <= 0.5
            intraday_range = (day_high - day_low) / prev_close * 100 if prev_close > 0 else 99.0
            # buying pressure
            _buy_days  = int(d.get("buy_days",  5))
            _sell_days = int(d.get("sell_days", 5))
            _today_up  = ltp > float(ohlc.get("open", ltp))
            _total_w   = _buy_days + _sell_days + 2
            _buy_w     = _buy_days + (2 if _today_up else 0)
            bs_ratio   = round(_buy_w / _total_w, 2) if _total_w > 0 else 0.5
            # news
            nd               = (news_data or {}).get(sym, {})
            news_sentiment   = nd.get("sentiment", 0.0)
            news_has_results = nd.get("has_results", False)
            news_headlines   = nd.get("headlines", [])
            rows.append({
                "symbol": sym, "ltp": ltp, "prev_close": prev_close,
                "pct_change": pct_change, "volume": volume, "avg_vol_20": avg_vol,
                "vol_surge": vol_surge,
                "dist_to_uc": round(dist_to_uc, 2), "dist_to_lc": round(dist_to_lc, 2),
                "upper_circuit": round(_uc, 2), "lower_circuit": round(_lc, 2),
                "high_52w": round(high_52w, 2), "low_52w": round(low_52w, 2),
                "dist_52w": dist_52w,
                "vwap": round(vwap, 2), "above_vwap": above_vwap,
                "rsi": round(rsi, 1),
                "bb_squeeze": d.get("bb_squeeze",   False),
                "nr7":        d.get("nr7",           False),
                "inside_bar": d.get("inside_bar",    False),
                "range_bo":   d.get("range_bo",      False),
                "vcp":        d.get("vcp",           False),
                "near_day_high": near_day_high,
                "intraday_range": round(intraday_range, 2),
                "above_ema21":  d.get("above_ema21",  False),
                "above_ema50":  d.get("above_ema50",  False),
                "above_ema200": d.get("above_ema200", False),
                "ema21": d.get("ema21", 0.0), "ema50": d.get("ema50", 0.0),
                "ema200": d.get("ema200", 0.0),
                "macd_bullish": d.get("macd_bullish", False),
                "adx": d.get("adx", 20.0),
                "up_days": d.get("up_days", 0),
                "bs_ratio": bs_ratio,
                "news_sentiment":   round(news_sentiment, 3),
                "news_has_results": news_has_results,
                "news_headlines":   news_headlines,
            })
        except Exception:
            continue
    return pd.DataFrame(rows)


# ── technical filter (identical logic to main screener) ───────────────────────

def technical_filter(df: pd.DataFrame,
                     use_pct: bool = True, min_pct: float = 2.0,
                     use_vol: bool = True, vol_min: float = 1.5,
                     use_rsi: bool = False, rsi_min: float = 30.0, rsi_max: float = 70.0,
                     use_vwap: bool = False, vwap_above: bool = True,
                     use_bs: bool = False, bs_min: float = 0.65,
                     use_bb_squeeze: bool = False, use_nr7: bool = False,
                     use_near_day_high: bool = False, use_tight_intraday: bool = False,
                     use_setup_rsi: bool = False, use_inside_bar: bool = False,
                     use_range_bo: bool = False, use_vcp: bool = False,
                     use_news: bool = False, news_results_only: bool = False,
                     use_ema21: bool = False, use_ema50: bool = False,
                     use_ema200: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    if use_pct:
        df = df[df["pct_change"] >= min_pct]
    if use_vol:
        df = df[df["vol_surge"] >= vol_min]
    if use_rsi and "rsi" in df.columns:
        df = df[(df["rsi"] >= rsi_min) & (df["rsi"] <= rsi_max)]
    if use_vwap and "above_vwap" in df.columns:
        df = df[df["above_vwap"] == vwap_above]
    if use_bs and "bs_ratio" in df.columns:
        df = df[df["bs_ratio"] >= bs_min]
    strategy_masks = []
    _col = lambda c: c in df.columns
    if use_bb_squeeze    and _col("bb_squeeze"):    strategy_masks.append(df["bb_squeeze"])
    if use_nr7           and _col("nr7"):           strategy_masks.append(df["nr7"])
    if use_near_day_high and _col("near_day_high"): strategy_masks.append(df["near_day_high"])
    if use_tight_intraday and _col("intraday_range"):
        strategy_masks.append(df["intraday_range"] <= 1.5)
    if use_setup_rsi and _col("rsi"):
        strategy_masks.append((df["rsi"] >= 45) & (df["rsi"] <= 60))
    if use_inside_bar and _col("inside_bar"): strategy_masks.append(df["inside_bar"])
    if use_range_bo   and _col("range_bo"):   strategy_masks.append(df["range_bo"])
    if use_vcp        and _col("vcp"):         strategy_masks.append(df["vcp"])
    if strategy_masks:
        combined = strategy_masks[0]
        for m in strategy_masks[1:]:
            combined = combined | m
        df = df[combined]
    if use_ema21  and _col("above_ema21"):  df = df[df["above_ema21"]  == True]  # noqa
    if use_ema50  and _col("above_ema50"):  df = df[df["above_ema50"]  == True]  # noqa
    if use_ema200 and _col("above_ema200"): df = df[df["above_ema200"] == True]  # noqa
    if use_news and _col("news_sentiment"):
        df = df[df["news_sentiment"] > 0.05]
    if news_results_only and _col("news_has_results"):
        df = df[df["news_has_results"] == True]  # noqa
    return df


# ── scoring (identical to main screener) ─────────────────────────────────────

def score_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    max_surge = max(df["vol_surge"].max(), 1)
    max_pct   = max(df["pct_change"].max(), 1)
    max_52w, min_52w = df["dist_52w"].max(), df["dist_52w"].min()
    df["vol_score"] = (df["vol_surge"]  / max_surge * 40).clip(0, 40)
    df["mom_score"] = (df["pct_change"] / max_pct   * 40).clip(0, 40)
    df["res_score"] = (((max_52w - df["dist_52w"]) / (max_52w - min_52w) * 20).clip(0, 20)
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
    sw = pd.Series(0.0, index=df.index)
    _c = lambda col: col in df.columns
    if _c("above_ema200"): sw += df["above_ema200"].astype(float) * 10
    if _c("above_ema50"):  sw += df["above_ema50"].astype(float)  * 10
    if _c("above_ema21"):  sw += df["above_ema21"].astype(float)  * 5
    if _c("above_ema21") and _c("above_ema50") and _c("above_ema200"):
        sw += (df["above_ema21"] & df["above_ema50"] & df["above_ema200"]).astype(float) * 5
    if _c("rsi"):
        rsi = df["rsi"]
        sw += ((rsi >= 50) & (rsi <= 65)).astype(float) * 25
        sw += ((rsi >= 65) & (rsi <= 72)).astype(float) * 18
        sw += ((rsi >= 45) & (rsi < 50)).astype(float) * 12
    if _c("vol_surge"): sw += (df["vol_surge"] >= 1.5).astype(float) * 20
    if _c("dist_52w"):  sw += (df["dist_52w"] <= 10).astype(float) * 15
    if _c("adx"):       sw += (df["adx"] >= 25).astype(float) * 10
    if _c("bb_squeeze"): sw += df["bb_squeeze"].astype(float) * 3
    df["swing_score"] = sw.clip(0, 100).round(1)
    return df


# ── render table (no buy/sell buttons) ───────────────────────────────────────

def render_table(final: pd.DataFrame, names: dict[str, str]) -> None:
    if final.empty:
        st.info("No stocks match current filters. Try relaxing the filters in the sidebar.")
        return

    if st.session_state.pop("_clear_search_free", False):
        st.session_state["free_tbl_search"] = ""

    sc, xc = st.columns([10, 1])
    with sc:
        st.text_input("🔍 Search symbol or name",
                      placeholder="e.g. Reliance, TCS, INFY",
                      key="free_tbl_search")
    with xc:
        st.markdown("<div style='margin-top:28px'>", unsafe_allow_html=True)
        if st.button("✕", key="free_clear_search", use_container_width=True):
            st.session_state["_clear_search_free"] = True
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    search = st.session_state.get("free_tbl_search", "").strip().lower()
    if search:
        mask = (final["symbol"].str.lower().str.contains(search, na=False) |
                final["symbol"].apply(lambda s: names.get(s, "").lower()).str.contains(search, na=False))
        final = final[mask]
        if final.empty:
            st.info(f"No results for '{search}'.")
            return

    HEADERS = [
        ("#",           None,          0.3),
        ("Symbol",      None,          1.0),
        ("Name",        None,          2.0),
        ("LTP ₹",       "ltp",         0.9),
        ("Chg %",       "pct_change",  0.9),
        ("Vol Surge",   "vol_surge",   0.9),
        ("VWAP 15m",    "vwap",        0.9),
        ("RSI",         "rsi",         0.7),
        ("52W High",    "high_52w",    0.9),
        ("ADX",         "adx",         0.7),
        ("Buy Press",   "bs_ratio",    0.7),
        ("Setup",       "setup_score", 1.1),
        ("Score",       "score",       0.7),
        ("Swing",       "swing_score", 0.7),
    ]
    sortable_cols = {h[1] for h in HEADERS if h[1]}

    _default_sort = "swing_score" if st.session_state.get("free_k_strategy") == "Swing Trade (3-5 day)" else "pct_change"
    if ("free_tbl_sort_col" not in st.session_state or
            st.session_state["free_tbl_sort_col"] not in sortable_cols):
        st.session_state["free_tbl_sort_col"] = _default_sort
        st.session_state["free_tbl_sort_asc"]  = False

    # header row
    widths = [h[2] for h in HEADERS]
    hcols  = st.columns(widths)
    for ci, (label, sort_key, _) in enumerate(HEADERS):
        with hcols[ci]:
            if sort_key:
                arrow = ""
                if st.session_state["free_tbl_sort_col"] == sort_key:
                    arrow = " ▲" if st.session_state["free_tbl_sort_asc"] else " ▼"
                if st.button(label.replace("\n", " ") + arrow,
                             key=f"free_hdr_{sort_key}",
                             use_container_width=True):
                    if st.session_state["free_tbl_sort_col"] == sort_key:
                        st.session_state["free_tbl_sort_asc"] = not st.session_state["free_tbl_sort_asc"]
                    else:
                        st.session_state["free_tbl_sort_col"] = sort_key
                        st.session_state["free_tbl_sort_asc"] = False
                    st.rerun()
            else:
                st.markdown(
                    f"<div style='font-size:11px;font-weight:700;color:#555;"
                    f"text-align:center;padding:4px 0'>{label.replace(chr(10), '<br>')}</div>",
                    unsafe_allow_html=True,
                )

    sort_col = st.session_state["free_tbl_sort_col"]
    sort_asc = st.session_state["free_tbl_sort_asc"]
    if sort_col in final.columns:
        final = final.sort_values(sort_col, ascending=sort_asc)

    for idx, (_, row) in enumerate(final.iterrows()):
        sym  = row["symbol"]
        name = names.get(sym, sym)
        ltp  = row["ltp"]
        pct  = row["pct_change"]
        pct_color = "#1b5e20" if pct >= 0 else "#b71c1c"
        rsi  = row.get("rsi", 50)
        rsi_color = "#1b5e20" if rsi < 40 else ("#b71c1c" if rsi > 70 else "#333")
        vwap = row.get("vwap", 0)
        above_vwap = row.get("above_vwap", None)
        vwap_col = "#1b5e20" if above_vwap else ("#b71c1c" if above_vwap is False else "#888")
        vol  = row.get("vol_surge", 1.0)
        adx  = row.get("adx", 20)
        bs   = row.get("bs_ratio", 0.5)
        score     = row.get("score", 0)
        swing     = row.get("swing_score", 0)
        score_col = "#1b5e20" if score >= 70 else ("#e65100" if score >= 40 else "#888")
        swing_col = "#1b5e20" if swing >= 70 else ("#e65100" if swing >= 40 else "#888")
        # setup signals
        signals = []
        if row.get("vcp"):        signals.append(("VCP",  "#7b1fa2"))
        if row.get("inside_bar"): signals.append(("IB",   "#0277bd"))
        if row.get("nr7"):        signals.append(("NR7",  "#00695c"))
        if row.get("bb_squeeze"): signals.append(("BBSQ", "#ef6c00"))
        if row.get("range_bo"):   signals.append(("RBO",  "#2e7d32"))
        if row.get("macd_bullish"): signals.append(("MACD↑", "#1565c0"))
        signal_html = " ".join(
            f"<span style='background:{c};color:#fff;font-size:9px;"
            f"border-radius:3px;padding:1px 4px'>{s}</span>"
            for s, c in signals
        )

        rcols = st.columns(widths)
        with rcols[0]:
            st.markdown(f"<div style='text-align:center;font-size:12px;color:#888;padding:6px 0'>{idx+1}</div>",
                        unsafe_allow_html=True)
        with rcols[1]:
            st.markdown(f"<div style='font-size:12px;font-weight:700;padding:6px 0'>{sym}</div>",
                        unsafe_allow_html=True)
        with rcols[2]:
            st.markdown(f"<div style='font-size:11px;color:#555;padding:6px 0'>{name[:22]}</div>",
                        unsafe_allow_html=True)
        with rcols[3]:
            st.markdown(f"<div style='font-size:13px;font-weight:700;padding:6px 0'>₹{ltp:,.2f}</div>",
                        unsafe_allow_html=True)
        with rcols[4]:
            st.markdown(f"<div style='font-size:12px;font-weight:700;color:{pct_color};padding:6px 0'>"
                        f"{pct:+.2f}%</div>", unsafe_allow_html=True)
        with rcols[5]:
            vol_col = "#1b5e20" if vol >= 2 else ("#e65100" if vol >= 1.5 else "#888")
            st.markdown(f"<div style='font-size:12px;color:{vol_col};padding:6px 0'>{vol:.1f}×</div>",
                        unsafe_allow_html=True)
        with rcols[6]:
            st.markdown(f"<div style='font-size:12px;color:{vwap_col};padding:6px 0'>₹{vwap:,.0f}</div>",
                        unsafe_allow_html=True)
        with rcols[7]:
            st.markdown(f"<div style='font-size:12px;color:{rsi_color};padding:6px 0'>{rsi:.0f}</div>",
                        unsafe_allow_html=True)
        with rcols[8]:
            st.markdown(f"<div style='font-size:11px;padding:6px 0'>₹{row.get('high_52w',0):,.0f}</div>",
                        unsafe_allow_html=True)
        with rcols[9]:
            adx_col = "#1b5e20" if adx >= 25 else "#888"
            st.markdown(f"<div style='font-size:12px;color:{adx_col};padding:6px 0'>{adx:.0f}</div>",
                        unsafe_allow_html=True)
        with rcols[10]:
            bs_col = "#1b5e20" if bs >= 0.65 else ("#b71c1c" if bs <= 0.4 else "#888")
            st.markdown(f"<div style='font-size:12px;color:{bs_col};padding:6px 0'>{bs*100:.0f}%</div>",
                        unsafe_allow_html=True)
        with rcols[11]:
            st.markdown(f"<div style='font-size:11px;padding:4px 0'>{signal_html or '—'}</div>",
                        unsafe_allow_html=True)
        with rcols[12]:
            st.markdown(f"<div style='font-size:13px;font-weight:800;color:{score_col};padding:6px 0'>"
                        f"{score:.0f}</div>", unsafe_allow_html=True)
        with rcols[13]:
            st.markdown(f"<div style='font-size:13px;font-weight:800;color:{swing_col};padding:6px 0'>"
                        f"{swing:.0f}</div>", unsafe_allow_html=True)

        st.markdown("<hr style='margin:0;border-color:#f0f0f0'>", unsafe_allow_html=True)


# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")
    st.info("🆓 Free mode — Yahoo Finance data (~15 min delay). No Zerodha API needed.")

    st.subheader("Universe")
    UNIVERSE_CHOICE = st.selectbox("Stock universe", ["Nifty F&O", "Nifty 500"],
                                   key="free_universe")

    def _on_free_strategy_change() -> None:
        s  = st.session_state["free_k_strategy"]
        sk = s.replace(" ", "_").replace("(", "").replace(")", "").replace("-", "") + "_free"
        is_swing = s == "Swing Trade (3-5 day)"
        is_bp    = s == "Buying Pressure"
        is_mom   = s == "Momentum (already moving)"
        is_vol   = s in ("Momentum (already moving)", "Range Breakout (5-day)", "Buying Pressure")
        st.session_state[f"k_pct_on_{sk}"]  = is_mom
        st.session_state[f"k_vol_on_{sk}"]  = is_vol
        st.session_state[f"k_rsi_on_{sk}"]  = is_swing
        st.session_state[f"k_rsi_rng_{sk}"] = (50, 72) if is_swing else (30, 70)
        st.session_state[f"k_vwap_on_{sk}"] = False
        st.session_state[f"k_bs_on_{sk}"]   = is_bp
        st.session_state[f"k_ema21_{sk}"]   = is_swing
        st.session_state[f"k_ema50_{sk}"]   = is_swing
        st.session_state[f"k_ema200_{sk}"]  = is_swing

    st.subheader("Strategy")
    STRATEGY = st.selectbox(
        "Select strategy",
        ["Swing Trade (3-5 day)", "Buying Pressure", "Momentum (already moving)",
         "Pre-Breakout (coiling)", "VCP Setup", "Inside Bar",
         "Range Breakout (5-day)", "Custom (manual filters)"],
        index=0,
        key="free_k_strategy",
        on_change=_on_free_strategy_change,
    )

    _STRATEGY_DESC = {
        "Swing Trade (3-5 day)":        "EMA aligned + RSI 50-65 + volume + near 52W high.",
        "Buying Pressure":              "65%+ of last 10 days were up-candles.",
        "Momentum (already moving)":    "% change ≥2% AND volume surge ≥1.5×.",
        "Pre-Breakout (coiling)":       "BB Squeeze OR NR7 OR VCP OR Inside Bar.",
        "VCP Setup":                    "3 successive daily range contractions.",
        "Inside Bar":                   "Today's range inside yesterday's.",
        "Range Breakout (5-day)":       "Close broke above 5-day high with volume.",
        "Custom (manual filters)":      "Configure every filter individually below.",
    }
    st.caption(_STRATEGY_DESC[STRATEGY])

    _S  = STRATEGY
    _sk = _S.replace(" ", "_").replace("(","").replace(")","").replace("-","") + "_free"
    IS_SWING           = _S == "Swing Trade (3-5 day)"
    IS_BUYING_PRESSURE = _S == "Buying Pressure"

    USE_BB_SQUEEZE     = _S in ("Pre-Breakout (coiling)",)
    USE_NR7            = _S in ("Pre-Breakout (coiling)",)
    USE_TIGHT_INTRADAY = _S in ("Pre-Breakout (coiling)",)
    USE_SETUP_RSI      = _S in ("Pre-Breakout (coiling)",)
    USE_INSIDE_BAR     = _S in ("Pre-Breakout (coiling)", "Inside Bar")
    USE_VCP            = _S in ("Pre-Breakout (coiling)", "VCP Setup")
    USE_RANGE_BO       = _S in ("Range Breakout (5-day)",)
    USE_NEAR_DAY_HIGH  = False

    if _S == "Custom (manual filters)":
        st.subheader("Pattern Signals (OR)")
        USE_BB_SQUEEZE     = st.toggle("BB Squeeze",           value=False, key=f"k_bbs_{_sk}")
        USE_NR7            = st.toggle("NR7 (Narrowest 7d)",   value=False, key=f"k_nr7_{_sk}")
        USE_NEAR_DAY_HIGH  = st.toggle("Near day's high",      value=False, key=f"k_ndh_{_sk}")
        USE_TIGHT_INTRADAY = st.toggle("Tight range (<1.5%)",  value=False, key=f"k_tir_{_sk}")
        USE_SETUP_RSI      = st.toggle("RSI building (45-60)", value=False, key=f"k_sri_{_sk}")
        USE_INSIDE_BAR     = st.toggle("Inside Bar",           value=False, key=f"k_ib_{_sk}")
        USE_RANGE_BO       = st.toggle("Range Breakout 5D",    value=False, key=f"k_rbo_{_sk}")
        USE_VCP            = st.toggle("VCP",                  value=False, key=f"k_vcp_{_sk}")

    st.subheader("Technical Filter")
    _pct_default = _S == "Momentum (already moving)"
    _vol_default = _S in ("Momentum (already moving)", "Range Breakout (5-day)", "Buying Pressure")
    _rsi_def     = IS_SWING
    _rsi_rng_def = (50, 72) if IS_SWING else (30, 70)

    USE_PCT_FILTER = st.toggle("Enable % change filter",     value=_pct_default, key=f"k_pct_on_{_sk}")
    MIN_PCT_CHANGE = st.slider("Min price change %", 0.5, 10.0, _DEF_MIN_PCT, 0.5,
                               disabled=not USE_PCT_FILTER, key=f"k_pct_val_{_sk}")
    USE_VOL_FILTER = st.toggle("Enable volume surge filter", value=_vol_default, key=f"k_vol_on_{_sk}")
    VOL_SURGE_MIN  = st.slider("Min volume surge ×", 1.0, 5.0, _DEF_VOL_SURGE, 0.25,
                               disabled=not USE_VOL_FILTER, key=f"k_vol_val_{_sk}")
    USE_RSI_FILTER = st.toggle("Enable RSI range filter",   value=_rsi_def,     key=f"k_rsi_on_{_sk}")
    RSI_RANGE      = st.slider("RSI range", 0, 100, _rsi_rng_def, 1,
                               disabled=not USE_RSI_FILTER, key=f"k_rsi_rng_{_sk}")
    RSI_MIN, RSI_MAX = RSI_RANGE
    USE_VWAP_FILTER = st.toggle("Enable VWAP filter (15m)", value=False, key=f"k_vwap_on_{_sk}")
    VWAP_SIDE       = st.selectbox("Price vs 15m VWAP", ["Above VWAP", "Below VWAP"],
                                   disabled=not USE_VWAP_FILTER, key=f"k_vwap_side_{_sk}")
    USE_BS_FILTER  = st.toggle("Enable Buy Pressure filter", value=IS_BUYING_PRESSURE,
                               key=f"k_bs_on_{_sk}")
    BS_RATIO_MIN   = st.slider("Min Buy Pressure %", 50, 100, 65, 5,
                               disabled=not USE_BS_FILTER, key=f"k_bs_val_{_sk}") / 100.0

    st.subheader("EMA Filter")
    USE_EMA21  = st.toggle("Above 21 EMA",  value=IS_SWING, key=f"k_ema21_{_sk}")
    USE_EMA50  = st.toggle("Above 50 EMA",  value=IS_SWING, key=f"k_ema50_{_sk}")
    USE_EMA200 = st.toggle("Above 200 EMA", value=IS_SWING, key=f"k_ema200_{_sk}")

    st.subheader("News & Results")
    USE_NEWS_FILTER    = st.toggle("Positive news sentiment", value=False, key=f"k_news_{_sk}")
    USE_RESULTS_FILTER = st.toggle("Has recent results",      value=False, key=f"k_results_{_sk}")

    st.subheader("Fundamental Filter")
    USE_FUNDAMENTAL   = st.toggle("Enable fundamental filter", value=False, key=f"k_fund_{_sk}")
    MARKET_CAP_MIN_CR = st.number_input("Min market cap (Cr)", value=_DEF_MARKET_CAP, step=500,
                                        disabled=not USE_FUNDAMENTAL)
    PE_MAX            = st.slider("Max PE ratio", 10, 120, _DEF_PE_MAX, 5,
                                  disabled=not USE_FUNDAMENTAL)
    EPS_MIN           = st.number_input("Min EPS", value=_DEF_EPS_MIN, step=0.5,
                                        disabled=not USE_FUNDAMENTAL)
    INST_MIN_PCT      = st.number_input("Min institutional %", value=_DEF_INST_MIN, step=5.0,
                                        disabled=not USE_FUNDAMENTAL)

    st.markdown("---")
    st.caption(f"Auto-refresh every {REFRESH_SECONDS} s")
    st.caption("🟢 Score ≥ 70  🟡 Score ≥ 40  ⚪ Score < 40")
    st.caption("⏱ Yahoo Finance prices have ~15 min delay")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    universe = load_universe(UNIVERSE_CHOICE)
    names: dict[str, str] = {}
    screened = list(universe)

    # Only load fundamentals (slow) when the filter is actually enabled
    if USE_FUNDAMENTAL:
        with st.spinner(f"Loading fundamentals for {len(universe)} stocks (cached daily)…"):
            fund_df = load_fundamentals(tuple(universe))
        if not fund_df.empty:
            if "symbol" in fund_df.columns and "name" in fund_df.columns:
                names = dict(zip(fund_df["symbol"], fund_df["name"].fillna("")))
            fund_pass, _ = fundamental_filter(
                fund_df, enabled=True,
                mc_min=MARKET_CAP_MIN_CR, pe_max=PE_MAX,
                eps_min=EPS_MIN, inst_min=INST_MIN_PCT,
            )
            screened = fund_pass["symbol"].tolist() if not fund_pass.empty else list(universe)
    else:
        fund_df = pd.DataFrame()

    with st.spinner(f"Loading daily data for {len(screened)} stocks (cached daily)…"):
        daily_data = load_daily_data(tuple(screened))

    if not daily_data:
        st.warning("⚠️ Daily data unavailable — yfinance may be rate-limited. Try again in a minute.")

    # cache quotes in session_state for 90s to avoid hammering yfinance
    _now       = time.time()
    _cache_age = _now - st.session_state.get("_free_quotes_ts", 0)
    if _cache_age > 90:
        with st.spinner(f"Fetching live prices for {len(screened)} stocks via Yahoo Finance…"):
            quotes = load_yf_quotes(tuple(screened))
        st.session_state["_free_quotes_cache"] = quotes
        st.session_state["_free_quotes_ts"]    = _now
    else:
        quotes = st.session_state.get("_free_quotes_cache", {})

    with st.spinner("Loading 15-min VWAP (cached 5 min)…"):
        vwap_15_data = load_15min_vwap(tuple(screened))

    if USE_NEWS_FILTER or USE_RESULTS_FILTER:
        with st.spinner("Loading news (cached 2 h)…"):
            news_data = load_news_data(tuple(screened))
    else:
        news_data = {}

    intraday  = build_intraday_df(quotes, daily_data, news_data, vwap_15_data)

    tech_pass = technical_filter(
        intraday,
        use_pct=USE_PCT_FILTER,  min_pct=MIN_PCT_CHANGE,
        use_vol=USE_VOL_FILTER,  vol_min=VOL_SURGE_MIN,
        use_rsi=USE_RSI_FILTER,  rsi_min=RSI_MIN, rsi_max=RSI_MAX,
        use_vwap=USE_VWAP_FILTER, vwap_above=(VWAP_SIDE == "Above VWAP"),
        use_bs=USE_BS_FILTER,    bs_min=BS_RATIO_MIN,
        use_bb_squeeze=USE_BB_SQUEEZE, use_nr7=USE_NR7,
        use_near_day_high=USE_NEAR_DAY_HIGH, use_tight_intraday=USE_TIGHT_INTRADAY,
        use_setup_rsi=USE_SETUP_RSI, use_inside_bar=USE_INSIDE_BAR,
        use_range_bo=USE_RANGE_BO, use_vcp=USE_VCP,
        use_news=USE_NEWS_FILTER, news_results_only=USE_RESULTS_FILTER,
        use_ema21=USE_EMA21, use_ema50=USE_EMA50, use_ema200=USE_EMA200,
    )

    final = score_df(tech_pass)

    # apply names from fundamentals (if loaded) or fall back to symbol
    if not final.empty:
        if names:
            final["name"] = final["symbol"].map(names).fillna(final["symbol"])
        else:
            final["name"] = final["symbol"]

    _finfo = []
    if USE_PCT_FILTER:   _finfo.append(f"Chg≥{MIN_PCT_CHANGE}%")
    if USE_VOL_FILTER:   _finfo.append(f"Vol≥{VOL_SURGE_MIN}×")
    if USE_RSI_FILTER:   _finfo.append(f"RSI {RSI_MIN}-{RSI_MAX}")
    if USE_EMA21:        _finfo.append("EMA21")
    if USE_EMA50:        _finfo.append("EMA50")
    if USE_EMA200:       _finfo.append("EMA200")
    _ftxt = " · ".join(_finfo) if _finfo else "no filters"

    st.info(
        f"**{STRATEGY}** | {_ftxt} | matched: **{len(tech_pass)}** / {len(intraday)} "
        f"| universe: {len(screened)} stocks | ⏱ ~15 min delayed"
    )

    if not quotes:
        st.warning("No quotes loaded yet. Yahoo Finance may be rate-limiting — wait 60s and refresh.")
        return

    render_table(final, names)


main()

# ── auto-refresh ──────────────────────────────────────────────────────────────
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="free_screener_autorefresh")
except ImportError:
    pass
