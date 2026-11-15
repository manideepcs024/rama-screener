"""
Market Overview — Breadth + PCR + Index Spot/Futures + ATM Options + VWAP + Patterns + TA + Recommendation
Auto-refreshes every 60 seconds.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from kiteconnect import KiteConnect  # type: ignore

_API_KEY    = os.getenv("ZERODHA_API_KEY", "")
_TOKEN_FILE = ROOT / "output" / "kite_token.json"


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
        st.error("Kite session expired. Please log in again from the main screener page.")
        st.stop()


# ── index config ──────────────────────────────────────────────────────────────
INDEX_CFG: dict[str, dict] = {
    "NIFTY": {
        "display":     "Nifty 50",
        "spot_sym":    "NSE:NIFTY 50",
        "seg":         "NFO",
        "strike_step": 50,
        "color":       "#1a73e8",
        "badge":       "N",
        "yf_sym":      "^NSEI",
        "nse_sym":     "NIFTY",
    },
    "BANKNIFTY": {
        "display":     "Bank Nifty",
        "spot_sym":    "NSE:NIFTY BANK",
        "seg":         "NFO",
        "strike_step": 100,
        "color":       "#e65100",
        "badge":       "B",
        "yf_sym":      "^NSEBANK",
        "nse_sym":     "BANKNIFTY",
    },
    "FINNIFTY": {
        "display":     "Fin Nifty",
        "spot_sym":    "NSE:NIFTY FIN SERVICE",
        "seg":         "NFO",
        "strike_step": 50,
        "color":       "#2e7d32",
        "badge":       "F",
        "yf_sym":      "NIFTY_FIN_SERVICE.NS",
        "nse_sym":     "FINNIFTY",
    },
    "SENSEX": {
        "display":     "Sensex",
        "spot_sym":    "BSE:SENSEX",
        "seg":         "BFO",
        "strike_step": 100,
        "color":       "#7b1fa2",
        "badge":       "S",
        "yf_sym":      "^BSESN",
        "nse_sym":     None,          # BSE — PCR not on NSE
    },
}

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
}

# ── NSE breadth only (advance/decline — not available via Kite) ───────────────

@st.cache_data(ttl=300, show_spinner=False)
def _fetch_nse_breadth() -> dict:
    """Nifty 50 advance/decline from NSE API."""
    import requests
    try:
        s = requests.Session()
        s.headers.update(_NSE_HEADERS)
        s.get("https://www.nseindia.com", timeout=10)
        r = s.get(
            "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050",
            timeout=10,
        )
        d = r.json()
        return {
            "adv":  int(d.get("advances",  0)),
            "dec":  int(d.get("declines",  0)),
            "unch": int(d.get("unchanged", 0)),
        }
    except Exception:
        return {}


# ── PCR via Kite quote() — uses live OI same as Zerodha app ──────────────────

@st.cache_data(ttl=120, show_spinner=False)
def _kite_pcr(seg: str, name: str, expiry: date) -> dict:
    """
    Calculates PCR for the given index + expiry using Kite quote() OI data.
    Matches what Zerodha displays — sum(PE OI) / sum(CE OI) across all strikes.
    """
    insts = _load_instruments(seg)
    # build tradingsymbol → type map for all CE/PE of this expiry
    opts: dict[str, str] = {
        f"{seg}:{i['tradingsymbol']}": i["instrument_type"]
        for i in insts
        if i["name"] == name
        and i["instrument_type"] in ("CE", "PE")
        and i["expiry"] == expiry
    }
    if not opts:
        return {"pcr": 0.0, "ce_oi": 0, "pe_oi": 0}

    kite   = _make_kite()
    ce_oi  = pe_oi = 0
    syms   = list(opts.keys())

    # quote() accepts up to 500 instruments; chunk to be safe
    for i in range(0, len(syms), 400):
        chunk = syms[i : i + 400]
        try:
            quotes = kite.quote(chunk)
            for sym, data in quotes.items():
                oi = int(data.get("oi", 0) or 0)
                if opts.get(sym) == "CE":
                    ce_oi += oi
                else:
                    pe_oi += oi
        except Exception:
            pass

    pcr = round(pe_oi / ce_oi, 2) if ce_oi else 0.0
    return {"pcr": pcr, "ce_oi": ce_oi, "pe_oi": pe_oi}


def _pcr_label(pcr: float) -> tuple[str, str]:
    """Returns (label, color) for a given PCR value."""
    if pcr <= 0:
        return "N/A", "#888"
    if pcr >= 1.3:
        return f"{pcr:.2f}  Bullish (heavy hedging)", "#1b5e20"
    if pcr >= 1.0:
        return f"{pcr:.2f}  Mildly Bullish", "#2e7d32"
    if pcr >= 0.8:
        return f"{pcr:.2f}  Neutral", "#e65100"
    return f"{pcr:.2f}  Bearish (call heavy)", "#b71c1c"


# ── instrument helpers ────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _load_instruments(seg: str) -> list[dict]:
    try:
        return _make_kite().instruments(seg)
    except Exception:
        return []


def _weekly_expiries(seg: str, name: str) -> list[date]:
    today = date.today()
    return sorted({
        i["expiry"]
        for i in _load_instruments(seg)
        if i["name"] == name
        and i["instrument_type"] in ("CE", "PE")
        and i["expiry"] >= today
    })[:4]


def _monthly_fut_expiry(seg: str, name: str) -> date | None:
    today = date.today()
    exp = sorted({
        i["expiry"]
        for i in _load_instruments(seg)
        if i["name"] == name and i["instrument_type"] == "FUT" and i["expiry"] >= today
    })
    return exp[0] if exp else None


def _fut_symbol(seg: str, name: str, expiry: date) -> str | None:
    for i in _load_instruments(seg):
        if i["name"] == name and i["instrument_type"] == "FUT" and i["expiry"] == expiry:
            return f"{seg}:{i['tradingsymbol']}"
    return None


def _opt_symbol(seg: str, name: str, expiry: date, strike: float, opt: str) -> str | None:
    for i in _load_instruments(seg):
        if (i["name"] == name and i["instrument_type"] == opt
                and i["expiry"] == expiry and i["strike"] == strike):
            return f"{seg}:{i['tradingsymbol']}"
    return None


def _opt_token(seg: str, name: str, expiry: date, strike: float, opt: str) -> int | None:
    for i in _load_instruments(seg):
        if (i["name"] == name and i["instrument_type"] == opt
                and i["expiry"] == expiry and i["strike"] == strike):
            return int(i["instrument_token"])
    return None


# ── 15-min intraday + VWAP + patterns ────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _fetch_15m(token: int) -> pd.DataFrame | None:
    try:
        today   = date.today()
        from_dt = datetime.combine(today, dtime(9, 15))
        to_dt   = datetime.now()
        raw     = _make_kite().historical_data(token, from_dt, to_dt, "15minute")
        if not raw:
            return None
        df = pd.DataFrame(raw)
        df.columns = [c.lower() for c in df.columns]
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["close"])
    except Exception:
        return None


def _calc_vwap(df: pd.DataFrame) -> float | None:
    if df is None or df.empty or df["volume"].sum() == 0:
        return None
    tp   = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (tp * df["volume"]).cumsum() / df["volume"].cumsum()
    return float(vwap.iloc[-1])


def _detect_patterns(df: pd.DataFrame) -> list[tuple[str, str, str]]:
    if df is None or len(df) < 4:
        return []
    h = df["high"].values
    l = df["low"].values
    r = h - l
    found: list[tuple[str, str, str]] = []
    if h[-1] < h[-2] and l[-1] > l[-2]:
        found.append(("Inside Bar", "Coiling inside prev candle — breakout pending", "#e65100"))
    if r[-1] == min(r[-4:]):
        found.append(("NR4", "Narrowest range in 4 bars — explosive move likely", "#7b1fa2"))
    if r[-4] > r[-3] > r[-2] > r[-1]:
        found.append(("VCP", "Volatility contraction — potential breakout", "#1a73e8"))
    return found


def _vwap_row_html(label: str, ltp: float | None, vwap: float | None,
                   pats: list[tuple[str, str, str]]) -> str:
    """
    Returns an HTML block for one option's VWAP row.
    LTP is green if above VWAP, red if below; shows pts and % distance.
    """
    if ltp is None:
        ltp_html = "<span style='color:#aaa'>N/A</span>"
        dist_html = ""
    elif vwap is None:
        col = "#1b5e20" if label == "CE" else "#b71c1c"
        ltp_html = f"<b style='color:{col}'>&#8377;{ltp:.2f}</b>"
        dist_html = "<span style='color:#aaa;font-size:11px'>VWAP N/A</span>"
    else:
        pts = ltp - vwap
        pct = pts / vwap * 100
        col = "#1b5e20" if pts >= 0 else "#b71c1c"
        sign = "+" if pts >= 0 else ""
        ltp_html = f"<b style='color:{col}'>&#8377;{ltp:.2f}</b>"
        dist_html = (
            f"<span style='color:{col};font-size:11px'>"
            f"({sign}{pts:.1f} pts, {sign}{pct:.1f}% vs VWAP)</span>"
        )

    vwap_html = f"VWAP <b>&#8377;{vwap:.2f}</b>" if vwap else "VWAP <span style='color:#aaa'>N/A</span>"

    pat_html = " ".join(
        f"<span style='display:inline-block;padding:1px 7px;border-radius:4px;"
        f"font-size:10px;font-weight:700;border:1px solid {c};color:{c}'>{n}</span>"
        for n, _, c in pats
    ) if pats else ""

    opt_col = "#1b5e20" if label == "CE" else "#b71c1c"
    return (
        f"<div style='margin:4px 0;font-size:12px;line-height:1.8'>"
        f"<b style='color:{opt_col}'>{label}</b> &nbsp;"
        f"{ltp_html} &nbsp; {vwap_html} &nbsp; {dist_html}"
        + (f"<br>&nbsp;&nbsp;&nbsp;&nbsp;{pat_html}" if pat_html else "")
        + "</div>"
    )


# ── daily TA ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _fetch_ta(yf_sym: str) -> dict:
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
            rsi_s   = pta.rsi(close, length=14)
            rsi_val = float(rsi_s.iloc[-1]) if rsi_s is not None else 50.0
            ema9    = float(pta.ema(close, length=9).iloc[-1])
            ema21   = float(pta.ema(close, length=21).iloc[-1])
            ema50   = float(pta.ema(close, length=50).iloc[-1])
        except Exception:
            adx_val = 20.0; rsi_val = 50.0
            ema9 = ema21 = ema50 = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else float(close.iloc[-1])
        cur  = float(close.iloc[-1])
        return {
            "adx": adx_val, "rsi": rsi_val,
            "ema9": ema9, "ema21": ema21, "ema50": ema50,
            "close": cur, "pct": (cur - prev) / prev * 100,
        }
    except Exception:
        return {}


def _analyze(ta: dict, spot: float) -> dict:
    adx  = ta.get("adx", 20)
    rsi  = ta.get("rsi", 50)
    ema9 = ta.get("ema9", spot)
    ema21= ta.get("ema21", spot)
    ema50= ta.get("ema50", spot)

    if adx >= 30:   strength, s_col = "STRONG TREND",   "#1b5e20"
    elif adx >= 25: strength, s_col = "TRENDING",        "#2e7d32"
    elif adx >= 20: strength, s_col = "WEAK TREND",      "#e65100"
    else:           strength, s_col = "CHOPPY/RANGING",  "#b71c1c"

    bull = bear = 0
    sigs = []

    def chk(cond, y, n):
        nonlocal bull, bear
        if cond: bull += 1; sigs.append(f"<span style='color:#1b5e20'>{y}</span>")
        else:    bear += 1; sigs.append(f"<span style='color:#b71c1c'>{n}</span>")

    chk(spot  > ema9,  "P&gt;EMA9",  "P&lt;EMA9")
    chk(ema9  > ema21, "EMA9&gt;21", "EMA9&lt;21")
    chk(spot  > ema50, "P&gt;EMA50", "P&lt;EMA50")
    if   rsi > 55: bull += 1; sigs.append(f"<span style='color:#1b5e20'>RSI {rsi:.0f}&gt;55</span>")
    elif rsi < 45: bear += 1; sigs.append(f"<span style='color:#b71c1c'>RSI {rsi:.0f}&lt;45</span>")
    else:          sigs.append(f"<span style='color:#888'>RSI {rsi:.0f}</span>")

    if   bull > bear: direction, d_col, d_em = "BULLISH", "#1b5e20", "▲"
    elif bear > bull: direction, d_col, d_em = "BEARISH", "#b71c1c", "▼"
    else:             direction, d_col, d_em = "NEUTRAL", "#888",    "—"

    return {
        "strength": strength, "s_col": s_col,
        "direction": direction, "d_col": d_col, "d_em": d_em,
        "adx": adx, "rsi": rsi, "signals": sigs,
        "is_trending": adx >= 25,
    }


def _recommend(trend: dict, ce_cur: float | None, pe_cur: float | None,
               ce_nxt: float | None, pe_nxt: float | None,
               cur_exp: date | None, nxt_exp: date | None) -> dict:
    if not trend.get("is_trending"):
        return {
            "action": "WAIT — market is choppy",
            "note":   "ADX < 25. Option buying risks premium decay. Consider iron condor or stay flat.",
            "color": "#888", "em": "WAIT",
        }
    direction = trend["direction"]
    adx       = trend["adx"]
    if direction == "NEUTRAL":
        return {
            "action": "WAIT — no clear direction",
            "note":   "Signals are mixed. Wait for confirmation.",
            "color": "#888", "em": "WAIT",
        }
    use_cur = adx >= 30
    otype   = "CE" if direction == "BULLISH" else "PE"
    ltp     = (ce_cur if direction == "BULLISH" else pe_cur) if use_cur \
              else (ce_nxt if direction == "BULLISH" else pe_nxt)
    exp     = cur_exp if use_cur else nxt_exp
    if not ltp or ltp <= 0:
        return {"action": "DATA UNAVAILABLE", "note": "Option premium not available.",
                "color": "#888", "em": "?"}
    exp_str = exp.strftime("%d %b") if exp else "?"
    return {
        "action": f"BUY ATM {otype}  |  Expiry: {exp_str}",
        "note":   (f"{trend['d_em']} {direction} — ADX {adx:.0f}, RSI {trend['rsi']:.0f}. "
                   f"{'Strong trend → current week.' if use_cur else 'Moderate trend → next week.'}"),
        "entry":  ltp,
        "sl":     round(ltp * 0.40, 2),
        "t1":     round(ltp * 1.50, 2),
        "t2":     round(ltp * 2.00, 2),
        "otype":  otype,
        "color":  "#1b5e20" if direction == "BULLISH" else "#b71c1c",
        "em":     "BUY",
    }


# ── PAGE ──────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Market Overview",
    page_icon="chart_with_upwards_trend",
    layout="wide",
)
from _auth import require_login; require_login()

try:
    from streamlit_autorefresh import st_autorefresh  # type: ignore
    st_autorefresh(interval=60_000, key="mo_refresh")
except ImportError:
    pass

st.markdown("""
<style>
.mo-title { text-align:center;font-size:26px;font-weight:800;letter-spacing:2px;color:#1a73e8; }
.mo-sub   { text-align:center;color:#888;font-size:13px;margin-top:0;margin-bottom:16px; }
.breadth-box {
    display:flex;flex-wrap:wrap;gap:12px;align-items:center;
    border:1px solid #e0e0e0;border-radius:12px;padding:14px 20px;
    background:#fff;box-shadow:0 1px 4px rgba(0,0,0,0.06);
    margin-bottom:20px;
}
.b-block { display:flex;flex-direction:column;align-items:center;min-width:70px; }
.b-num   { font-size:22px;font-weight:800; }
.b-lbl   { font-size:11px;color:#888;font-weight:600;margin-top:1px; }
.b-bar-wrap { flex:1;min-width:160px;height:12px;border-radius:6px;
              background:#fdecea;overflow:hidden; }
.b-bar-adv  { height:100%;border-radius:6px;background:#1b5e20; }
.idx-badge {
    display:inline-flex;align-items:center;justify-content:center;
    width:30px;height:30px;border-radius:8px;font-weight:800;
    font-size:14px;color:#fff;margin-right:8px;flex-shrink:0;
}
.opt-pill {
    display:inline-block;padding:3px 10px;border-radius:6px;
    font-size:13px;font-weight:600;margin:3px 4px 3px 0;
}
.rec-box { border-left:5px solid;border-radius:10px;padding:12px 18px;
           margin-top:10px;background:#f9f9f9; }
.rec-action { font-size:15px;font-weight:800; }
.rec-note   { font-size:13px;color:#555;margin-top:3px; }
.metric-lbl { font-size:11px;color:#888;font-weight:600; }
.metric-val { font-size:17px;font-weight:800; }
hr.sep { border:none;border-top:1px solid #ececec;margin:4px 0 16px 0; }
div[data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
div[data-testid="stMarkdownContainer"] > p { margin-bottom: 0 !important; }
div[data-testid="stMarkdownContainer"] > div { margin-bottom: 0 !important; }
#flonnect-my-app { display: none !important; }
</style>
""", unsafe_allow_html=True)

st.markdown("<p class='mo-title'>Market Overview</p>", unsafe_allow_html=True)
st.markdown(
    f"<p class='mo-sub'>Breadth · PCR · ATM Options · VWAP · Patterns · TA · Recommendation"
    f" &nbsp;|&nbsp; {datetime.now().strftime('%d %b %Y  %H:%M')}</p>",
    unsafe_allow_html=True,
)

# ── market breadth header ─────────────────────────────────────────────────────
with st.spinner("Fetching NSE breadth..."):
    breadth = _fetch_nse_breadth()
adv     = breadth.get("adv",  0)
dec     = breadth.get("dec",  0)
unch    = breadth.get("unch", 0)
total   = adv + dec + unch or 1
adv_pct = adv / total * 100

if adv or dec:
    ad_ratio  = f"{adv}:{dec}" if dec else f"{adv}:0"
    ad_color  = "#1b5e20" if adv > dec else ("#b71c1c" if dec > adv else "#e65100")
    mood      = "Broadly Bullish" if adv_pct >= 60 else ("Broadly Bearish" if adv_pct <= 40 else "Mixed")
    mood_col  = "#1b5e20" if adv_pct >= 60 else ("#b71c1c" if adv_pct <= 40 else "#e65100")
    bar_w     = int(adv_pct)

    st.markdown(f"""
    <div class='breadth-box'>
        <div class='b-block'>
            <span class='b-num' style='color:#1b5e20'>{adv}</span>
            <span class='b-lbl'>Advances</span>
        </div>
        <div class='b-block'>
            <span class='b-num' style='color:#b71c1c'>{dec}</span>
            <span class='b-lbl'>Declines</span>
        </div>
        <div class='b-block'>
            <span class='b-num' style='color:#888'>{unch}</span>
            <span class='b-lbl'>Unchanged</span>
        </div>
        <div style='flex:1;min-width:140px'>
            <div style='font-size:11px;color:#888;margin-bottom:4px;font-weight:600'>
                Nifty 50 Breadth &nbsp;
                <span style='color:{ad_color};font-weight:700'>{ad_ratio}</span>
            </div>
            <div class='b-bar-wrap'>
                <div class='b-bar-adv' style='width:{bar_w}%'></div>
            </div>
            <div style='font-size:11px;margin-top:3px;color:{mood_col};font-weight:700'>{mood}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
else:
    st.info("Market breadth data unavailable (NSE API). Showing index cards below.")

# ── spot prices ───────────────────────────────────────────────────────────────
kite = get_kite()
spot_syms = [cfg["spot_sym"] for cfg in INDEX_CFG.values()]
try:
    spot_q = kite.ltp(spot_syms)
except Exception as e:
    st.error(f"Failed to fetch spot prices: {e}")
    st.stop()

# ── index cards ───────────────────────────────────────────────────────────────
for idx_name, cfg in INDEX_CFG.items():
    spot = float(spot_q.get(cfg["spot_sym"], {}).get("last_price", 0.0))
    if spot <= 0:
        st.warning(f"{cfg['display']}: spot price unavailable.")
        continue

    seg  = cfg["seg"]
    step = cfg["strike_step"]
    atm  = round(spot / step) * step

    exp_list   = _weekly_expiries(seg, idx_name)
    fut_expiry = _monthly_fut_expiry(seg, idx_name)
    cur_exp    = exp_list[0] if len(exp_list) > 0 else None
    nxt_exp    = exp_list[1] if len(exp_list) > 1 else None

    # symbols
    fut_sym    = _fut_symbol(seg, idx_name, fut_expiry) if fut_expiry else None
    ce_cur_sym = _opt_symbol(seg, idx_name, cur_exp,  atm, "CE") if cur_exp else None
    pe_cur_sym = _opt_symbol(seg, idx_name, cur_exp,  atm, "PE") if cur_exp else None
    ce_nxt_sym = _opt_symbol(seg, idx_name, nxt_exp,  atm, "CE") if nxt_exp else None
    pe_nxt_sym = _opt_symbol(seg, idx_name, nxt_exp,  atm, "PE") if nxt_exp else None

    # instrument tokens for 15-min data
    ce_tok = _opt_token(seg, idx_name, cur_exp, atm, "CE") if cur_exp else None
    pe_tok = _opt_token(seg, idx_name, cur_exp, atm, "PE") if cur_exp else None

    # batch LTP
    to_fetch = [s for s in [fut_sym, ce_cur_sym, pe_cur_sym, ce_nxt_sym, pe_nxt_sym] if s]
    try:
        q = kite.ltp(to_fetch) if to_fetch else {}
    except Exception:
        q = {}

    def _ltp(sym): return float(q[sym]["last_price"]) if sym and sym in q else None

    fut_ltp    = _ltp(fut_sym)
    ce_cur_ltp = _ltp(ce_cur_sym)
    pe_cur_ltp = _ltp(pe_cur_sym)
    ce_nxt_ltp = _ltp(ce_nxt_sym)
    pe_nxt_ltp = _ltp(pe_nxt_sym)

    # 15-min data, VWAP, patterns
    ce_df  = _fetch_15m(ce_tok) if ce_tok else None
    pe_df  = _fetch_15m(pe_tok) if pe_tok else None
    ce_vwap = _calc_vwap(ce_df)
    pe_vwap = _calc_vwap(pe_df)
    ce_pats = _detect_patterns(ce_df)
    pe_pats = _detect_patterns(pe_df)

    # daily TA
    ta    = _fetch_ta(cfg["yf_sym"])
    pct   = ta.get("pct", 0.0)
    p_col = "#1b5e20" if pct >= 0 else "#b71c1c"
    p_arr = "▲" if pct >= 0 else "▼"
    trend = _analyze(ta, spot) if ta else {}
    rec   = _recommend(trend, ce_cur_ltp, pe_cur_ltp, ce_nxt_ltp, pe_nxt_ltp, cur_exp, nxt_exp)

    # PCR via Kite OI (current week expiry, same as Zerodha app)
    pcr_data = _kite_pcr(seg, idx_name, cur_exp) if cur_exp else {}
    pcr_val  = pcr_data.get("pcr", 0.0)
    ce_oi_t  = pcr_data.get("ce_oi", 0)
    pe_oi_t  = pcr_data.get("pe_oi", 0)
    pcr_lbl, pcr_col = _pcr_label(pcr_val)

    # ── render card ───────────────────────────────────────────────────────────
    with st.container():
        # header
        def _fmt_oi(v: int) -> str:
            if v >= 10_000_000: return f"{v/10_000_000:.1f}Cr"
            if v >= 100_000:    return f"{v/100_000:.1f}L"
            if v >= 1_000:      return f"{v/1_000:.0f}K"
            return str(v)

        oi_detail = (
            f"&nbsp;<span style='color:#1b5e20;font-size:11px'>CE {_fmt_oi(ce_oi_t)}</span>"
            f"<span style='color:#888;font-size:11px'> / </span>"
            f"<span style='color:#b71c1c;font-size:11px'>PE {_fmt_oi(pe_oi_t)}</span>"
            if ce_oi_t or pe_oi_t else ""
        )

        st.markdown(
            f"<div style='display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:10px'>"
            f"<span class='idx-badge' style='background:{cfg['color']}'>{cfg['badge']}</span>"
            f"<span style='font-size:17px;font-weight:700'>{cfg['display']}</span>"
            f"<span style='font-size:22px;font-weight:800;margin-left:8px'>&#8377;{spot:,.2f}</span>"
            f"<span style='font-size:14px;font-weight:600;color:{p_col}'>{p_arr} {abs(pct):.2f}%</span>"
            f"<span style='margin-left:8px;padding:3px 10px;border-radius:6px;"
            f"background:#f0f4ff;font-size:12px;color:{pcr_col};font-weight:700'>"
            f"PCR {pcr_lbl}{oi_detail}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        col_sf, col_opt, col_ta = st.columns([1.1, 1.8, 1.3])

        # Spot / Futures
        with col_sf:
            st.markdown("**Spot / Futures**")
            spot_html = f"<div style='font-size:13px'>Spot &nbsp;<b>&#8377;{spot:,.2f}</b>"
            if fut_ltp and fut_expiry:
                prem = fut_ltp - spot
                pc = "#1b5e20" if prem >= 0 else "#b71c1c"
                spot_html += (
                    f"<br>Fut ({fut_expiry.strftime('%d %b')}) <b>&#8377;{fut_ltp:,.2f}</b> "
                    f"<span style='color:{pc};font-size:12px'>"
                    f"({'+' if prem>=0 else ''}{prem:.2f})</span>"
                )
            else:
                spot_html += "<br><span style='color:#aaa;font-size:12px'>Futures: N/A</span>"
            st.markdown(spot_html + "</div>", unsafe_allow_html=True)

        # ATM Options + VWAP + Patterns
        with col_opt:
            st.markdown(
                f"**ATM Options** <span style='color:#888;font-size:12px'>Strike {atm:,.0f}</span>",
                unsafe_allow_html=True,
            )

            def pill(val, otype):
                bg = "#e8f5e9" if otype == "CE" else "#fdecea"
                fg = "#1b5e20" if otype == "CE" else "#b71c1c"
                v  = f"&#8377;{val:.2f}" if val else "N/A"
                return f"<span class='opt-pill' style='background:{bg};color:{fg}'>{otype} {v}</span>"

            if cur_exp:
                st.markdown(
                    f"<div style='font-size:12px;color:#555;margin-bottom:2px'>"
                    f"This week ({cur_exp.strftime('%d %b')})</div>"
                    + pill(ce_cur_ltp, "CE") + pill(pe_cur_ltp, "PE"),
                    unsafe_allow_html=True,
                )
            if nxt_exp:
                st.markdown(
                    f"<div style='font-size:12px;color:#555;margin:5px 0 2px 0'>"
                    f"Next week ({nxt_exp.strftime('%d %b')})</div>"
                    + pill(ce_nxt_ltp, "CE") + pill(pe_nxt_ltp, "PE"),
                    unsafe_allow_html=True,
                )

            # VWAP + patterns
            st.markdown(
                "<div style='margin-top:8px;font-size:11px;font-weight:700;"
                "color:#555;text-transform:uppercase;letter-spacing:0.5px'>"
                "15-min VWAP &amp; Patterns</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                _vwap_row_html("CE", ce_cur_ltp, ce_vwap, ce_pats) +
                _vwap_row_html("PE", pe_cur_ltp, pe_vwap, pe_pats),
                unsafe_allow_html=True,
            )

        # Daily TA
        with col_ta:
            if trend:
                st.markdown("**Trend (Daily TA)**")
                st.markdown(
                    f"<div style='font-size:13px'>"
                    f"ADX <b style='color:{trend['s_col']}'>"
                    f"{trend['adx']:.0f} — {trend['strength']}</b><br>"
                    f"Direction <b style='color:{trend['d_col']}'>"
                    f"{trend['d_em']} {trend['direction']}</b><br>"
                    f"RSI <b>{trend['rsi']:.0f}</b></div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    "<div style='font-size:11px;line-height:1.9'>"
                    + " · ".join(trend["signals"]) + "</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown("<span style='color:#aaa;font-size:12px'>TA data unavailable</span>",
                            unsafe_allow_html=True)

        # Recommendation
        st.markdown(
            f"<div class='rec-box' style='border-color:{rec['color']}'>"
            f"<div class='rec-action' style='color:{rec['color']}'>"
            f"{rec['em']} &nbsp; {rec['action']}</div>"
            f"<div class='rec-note'>{rec.get('note','')}</div></div>",
            unsafe_allow_html=True,
        )
        if rec.get("entry"):
            entry = rec["entry"]; sl = rec["sl"]; t1 = rec["t1"]; t2 = rec["t2"]
            m1, m2, m3, m4 = st.columns(4)
            with m1:
                st.markdown(f"<div class='metric-lbl'>ENTRY</div>"
                            f"<div class='metric-val'>&#8377;{entry:.2f}</div>",
                            unsafe_allow_html=True)
            with m2:
                st.markdown(f"<div class='metric-lbl'>STOP LOSS</div>"
                            f"<div class='metric-val' style='color:#b71c1c'>&#8377;{sl:.2f} "
                            f"<span style='font-size:12px'>(-{(entry-sl)/entry*100:.0f}%)</span></div>",
                            unsafe_allow_html=True)
            with m3:
                st.markdown(f"<div class='metric-lbl'>TARGET 1</div>"
                            f"<div class='metric-val' style='color:#1b5e20'>&#8377;{t1:.2f} "
                            f"<span style='font-size:12px'>(+{(t1-entry)/entry*100:.0f}%)</span></div>",
                            unsafe_allow_html=True)
            with m4:
                st.markdown(f"<div class='metric-lbl'>TARGET 2</div>"
                            f"<div class='metric-val' style='color:#1b5e20'>&#8377;{t2:.2f} "
                            f"<span style='font-size:12px'>(+{(t2-entry)/entry*100:.0f}%)</span></div>",
                            unsafe_allow_html=True)

        st.markdown("<hr class='sep'>", unsafe_allow_html=True)
