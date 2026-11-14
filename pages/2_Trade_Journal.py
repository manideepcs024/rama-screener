"""
Trade Journal & History Dashboard
- All trades taken (open + closed, live + paper)
- Journal notes per trade
- P&L summary stats
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# ── paths ─────────────────────────────────────────────────────────────────────
_ROOT           = Path(__file__).resolve().parent.parent
POSITIONS_FILE  = _ROOT / "output" / "rama_positions.json"
JOURNAL_FILE    = _ROOT / "output" / "trade_journal.json"
MANUAL_FILE     = _ROOT / "output" / "manual_journal.json"

st.set_page_config(page_title="Trade Journal", page_icon="📓", layout="wide")

# ── helpers ───────────────────────────────────────────────────────────────────
def _load_positions() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _load_journal() -> dict:
    if not JOURNAL_FILE.exists():
        return {}
    try:
        with open(JOURNAL_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_journal(journal: dict) -> None:
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_FILE, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, indent=2, ensure_ascii=False, default=str)


def _load_manual() -> list:
    if not MANUAL_FILE.exists():
        return []
    try:
        with open(MANUAL_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def _save_manual(entries: list) -> None:
    MANUAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MANUAL_FILE, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False, default=str)


def _journal_key(sym: str, buy_price: float) -> str:
    return f"{sym}@{buy_price:.2f}"


def _fmt_dt(raw: str) -> str:
    try:
        return datetime.fromisoformat(raw).strftime("%d %b %Y  %H:%M")
    except Exception:
        return raw or "—"


# ── load data ─────────────────────────────────────────────────────────────────
positions = _load_positions()
journal   = _load_journal()

# ── build flat trade list ─────────────────────────────────────────────────────
rows = []
for sym, pos in positions.items():
    buy_fill  = next((f for f in pos.get("fills", []) if f.get("action") == "BUY"), {})
    sell_fill = next((f for f in pos.get("fills", []) if f.get("action") == "SELL"), {})
    buy_price = pos.get("buy_price", 0.0)
    sell_price = sell_fill.get("price") if sell_fill else None
    qty        = pos.get("qty", 0)
    pnl        = round((sell_price - buy_price) * qty, 2) if sell_price else None
    pnl_pct    = round((sell_price - buy_price) / buy_price * 100, 2) if sell_price and buy_price else None
    jkey       = _journal_key(sym, buy_price)
    jentry     = journal.get(jkey, {})
    rows.append({
        "symbol":     sym,
        "name":       pos.get("name", sym),
        "status":     pos.get("status", "closed"),
        "paper":      pos.get("paper", False),
        "qty":        qty,
        "buy_price":  buy_price,
        "sell_price": sell_price,
        "sl":         pos.get("sl"),
        "target":     pos.get("target"),
        "pnl":        pnl,
        "pnl_pct":    pnl_pct,
        "buy_time":   _fmt_dt(buy_fill.get("time", "")),
        "sell_time":  _fmt_dt(sell_fill.get("time", "")) if sell_fill else "—",
        "note":       jentry.get("note", ""),
        "note_updated": jentry.get("updated", ""),
        "jkey":       jkey,
    })

df = pd.DataFrame(rows) if rows else pd.DataFrame()

# ── page header ───────────────────────────────────────────────────────────────
st.title("📓 Trade Journal & History")

if df.empty:
    st.info("No screener trades recorded yet. Start trading from the main screener.")

# ── summary stats ─────────────────────────────────────────────────────────────
if not df.empty:
    closed   = df[df["status"] == "closed"]
    open_pos = df[df["status"] == "open"]
    paper    = df[df["paper"] == True]
    live     = df[df["paper"] == False]

    closed_with_pnl = closed[closed["pnl"].notna()]
    wins  = closed_with_pnl[closed_with_pnl["pnl"] > 0]
    loss  = closed_with_pnl[closed_with_pnl["pnl"] < 0]
    total_pnl    = closed_with_pnl["pnl"].sum()
    win_rate     = len(wins) / len(closed_with_pnl) * 100 if len(closed_with_pnl) > 0 else 0
    avg_win      = wins["pnl"].mean() if len(wins) > 0 else 0
    avg_loss     = loss["pnl"].mean() if len(loss) > 0 else 0
    rr_ratio     = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    _pnl_color = "green" if total_pnl >= 0 else "red"
    col1.metric("Total Trades",  len(df))
    col2.metric("Open",          len(open_pos))
    col3.metric("Closed",        len(closed_with_pnl))
    col4.metric("Win Rate",      f"{win_rate:.0f}%",
                delta=f"{len(wins)}W / {len(loss)}L")
    col5.metric("Total P&L",     f"₹{total_pnl:+,.0f}",
                delta=f"Avg win ₹{avg_win:+,.0f}")
    col6.metric("Reward:Risk",   f"{rr_ratio:.2f}x",
                delta="avg win / avg loss")

    st.markdown("---")

fdf = df.copy()
# ── filters ───────────────────────────────────────────────────────────────────
if not df.empty:
    fc1, fc2, fc3 = st.columns([2, 2, 2])
    with fc1:
        _filter_status = st.radio("Status", ["All", "Open", "Closed"],
                                  horizontal=True, key="jf_status")
    with fc2:
        _filter_type = st.radio("Type", ["All", "Live", "Paper"],
                                horizontal=True, key="jf_type")
    with fc3:
        _filter_note = st.checkbox("Only with journal notes", key="jf_note")

    if _filter_status == "Open":
        fdf = fdf[fdf["status"] == "open"]
    elif _filter_status == "Closed":
        fdf = fdf[fdf["status"] == "closed"]
    if _filter_type == "Live":
        fdf = fdf[fdf["paper"] == False]
    elif _filter_type == "Paper":
        fdf = fdf[fdf["paper"] == True]
    if _filter_note:
        fdf = fdf[fdf["note"].str.strip() != ""]

    st.caption(f"Showing {len(fdf)} of {len(df)} trades")

# ── trade cards ───────────────────────────────────────────────────────────────
for _, row in fdf.iterrows() if not fdf.empty else [].__iter__():
    sym    = row["symbol"]
    jkey   = row["jkey"]
    is_open   = row["status"] == "open"
    is_paper  = row["paper"]
    has_note  = bool(row["note"].strip())
    pnl       = row["pnl"]
    pnl_pct   = row["pnl_pct"]

    if is_open:
        border = "#2196f3"
        bg     = "#e3f2fd"
        status_badge = "<span style='background:#2196f3;color:white;border-radius:4px;padding:2px 8px;font-size:12px'>OPEN</span>"
    elif pnl and pnl > 0:
        border = "#4caf50"
        bg     = "#f1f8f1"
        status_badge = "<span style='background:#4caf50;color:white;border-radius:4px;padding:2px 8px;font-size:12px'>WIN</span>"
    elif pnl and pnl < 0:
        border = "#f44336"
        bg     = "#fff1f1"
        status_badge = "<span style='background:#f44336;color:white;border-radius:4px;padding:2px 8px;font-size:12px'>LOSS</span>"
    else:
        border = "#9e9e9e"
        bg     = "#fafafa"
        status_badge = "<span style='background:#9e9e9e;color:white;border-radius:4px;padding:2px 8px;font-size:12px'>CLOSED</span>"

    paper_badge = ("<span style='background:#ff9800;color:white;border-radius:4px;"
                   "padding:2px 8px;font-size:12px;margin-left:6px'>PAPER</span>") if is_paper else ""
    note_badge  = ("<span style='background:#7c4dff;color:white;border-radius:4px;"
                   "padding:2px 8px;font-size:12px;margin-left:6px'>📝</span>") if has_note else ""

    if pnl is not None:
        _pnl_color = "#1b5e20" if pnl >= 0 else "#b71c1c"
        pnl_str = f"<span style='color:{_pnl_color};font-weight:700'>₹{pnl:+,.2f} ({pnl_pct:+.2f}%)</span>"
    else:
        pnl_str = ""

    sell_html = (f"<span>Sold <b>₹{row['sell_price']:.2f}</b> · {row['sell_time']}</span>"
                 if row["sell_price"] else "")

    st.markdown(
        f"<div style='border:2px solid {border};border-radius:10px;"
        f"padding:14px 18px;margin:6px 0;background:{bg}'>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap'>"
        f"<span style='font-size:18px;font-weight:700'>{sym}</span>"
        f"<span style='color:#555;font-size:13px'>{row['name'][:35]}</span>"
        f"{status_badge}{paper_badge}{note_badge}"
        f"</div>"
        f"<div style='margin-top:6px;font-size:13px;color:#333;display:flex;flex-wrap:wrap;gap:16px'>"
        f"<span>Qty: <b>{row['qty']}</b></span>"
        f"<span>Buy: <b>₹{row['buy_price']:.2f}</b> · {row['buy_time']}</span>"
        f"{sell_html}"
        f"<span>SL: ₹{row['sl']:.2f}</span>"
        f"<span>Target: ₹{row['target']:.2f}</span>"
        f"{pnl_str}"
        f"</div></div>",
        unsafe_allow_html=True,
    )

    # ── journal section below each card ──────────────────────────────────────
    _note_state_key = f"jnl_{jkey}"
    if _note_state_key not in st.session_state:
        st.session_state[_note_state_key] = row["note"]

    jcol1, jcol2 = st.columns([6, 1])
    with jcol1:
        if has_note and not st.session_state.get(f"jnl_edit_{jkey}", False):
            st.markdown(
                f"<div style='margin:-4px 0 8px 4px;padding:8px 14px;"
                f"background:#ede7f6;border-left:4px solid #7c4dff;"
                f"border-radius:4px;font-size:13px;color:#333'>"
                f"📝 <b>Journal:</b> {row['note']}"
                + (f"<br><span style='font-size:11px;color:#888'>Saved {row['note_updated']}</span>" if row["note_updated"] else "")
                + "</div>",
                unsafe_allow_html=True,
            )
        else:
            def _save_note(_k=jkey, _nk=_note_state_key):
                _text = st.session_state.get(_nk, "").strip()
                _j    = _load_journal()
                _j[_k] = {"note": _text, "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
                _save_journal(_j)
                st.session_state[f"jnl_edit_{_k}"] = False
                st.session_state["_jnl_saved"] = _k

            st.text_area(
                "Journal note", key=_note_state_key, height=100,
                placeholder="Why did you enter? What was the plan? What went right/wrong?",
                label_visibility="collapsed",
            )
            bc1, bc2 = st.columns([1, 5])
            bc1.button("💾 Save", key=f"jnl_save_{jkey}", on_click=_save_note,
                       type="primary", use_container_width=True)
            if st.session_state.pop("_jnl_saved", None) == jkey:
                st.success("Saved ✓")

    with jcol2:
        _edit_label = "✏️ Edit" if has_note else "📝 Add note"
        if st.button(_edit_label, key=f"jnl_edit_btn_{jkey}", use_container_width=True):
            st.session_state[f"jnl_edit_{jkey}"] = True
            st.rerun()

        # delete journal note
        if has_note:
            if st.session_state.get(f"jnl_del_confirm_{jkey}"):
                dc1, dc2 = st.columns(2)
                if dc1.button("✓ Yes", key=f"jnl_del_yes_{jkey}", use_container_width=True):
                    _j = _load_journal()
                    _j.pop(jkey, None)
                    _save_journal(_j)
                    st.session_state.pop(f"jnl_del_confirm_{jkey}", None)
                    st.rerun()
                if dc2.button("✗ No", key=f"jnl_del_no_{jkey}", use_container_width=True):
                    st.session_state.pop(f"jnl_del_confirm_{jkey}", None)
                    st.rerun()
            else:
                if st.button("🗑 Note", key=f"jnl_del_btn_{jkey}", use_container_width=True):
                    st.session_state[f"jnl_del_confirm_{jkey}"] = True
                    st.rerun()

        # delete entire trade record
        if st.session_state.get(f"trade_del_confirm_{sym}"):
            dc1, dc2 = st.columns(2)
            if dc1.button("✓ Delete", key=f"trade_del_yes_{sym}",
                          use_container_width=True, type="primary"):
                _pos = _load_positions()
                _pos.pop(sym, None)
                POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(POSITIONS_FILE, "w", encoding="utf-8") as _fh:
                    import json as _json
                    _json.dump(_pos, _fh, indent=2, default=str)
                # also remove journal entry
                _j = _load_journal()
                _j.pop(jkey, None)
                _save_journal(_j)
                st.session_state.pop(f"trade_del_confirm_{sym}", None)
                st.rerun()
            if dc2.button("✗ Cancel", key=f"trade_del_no_{sym}", use_container_width=True):
                st.session_state.pop(f"trade_del_confirm_{sym}", None)
                st.rerun()
        else:
            if st.button("🗑 Trade", key=f"trade_del_btn_{sym}", use_container_width=True):
                st.session_state[f"trade_del_confirm_{sym}"] = True
                st.rerun()

# ── manual journal ────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📒 Manual Journal — Other Accounts")
st.caption("Add trades from Zerodha Kite app, Dhan, Upstox, or any other account.")

manual_entries = _load_manual()

# ── add new entry form ────────────────────────────────────────────────────────
with st.expander("➕ Add New Manual Trade", expanded=True):
    mc1, mc2, mc3 = st.columns(3)
    with mc1:
        m_symbol   = st.text_input("Symbol", placeholder="e.g. RELIANCE", key="m_sym").upper().strip()
        m_account  = st.text_input("Account / Broker", placeholder="e.g. Kite, Dhan", key="m_acc")
    with mc2:
        m_buy      = st.number_input("Buy Price ₹", min_value=0.01, value=100.0, step=0.05, key="m_buy", format="%.2f")
        m_sell     = st.number_input("Sell Price ₹ (0 = still open)", min_value=0.0, value=0.0, step=0.05, key="m_sell", format="%.2f")
    with mc3:
        m_qty      = st.number_input("Qty", min_value=1, value=1, step=1, key="m_qty")
        m_date     = st.date_input("Trade Date", key="m_date")
    m_note = st.text_area("Notes", placeholder="Why did you enter? What was the plan?", height=80, key="m_note")

    if st.button("💾 Save Entry", key="m_save", type="primary"):
        if not m_symbol:
            st.warning("Enter a symbol.")
        else:
            sell_price = float(m_sell) if m_sell > 0 else None
            pnl = round((sell_price - float(m_buy)) * int(m_qty), 2) if sell_price else None
            entry = {
                "id":       len(manual_entries) + 1,
                "symbol":   m_symbol,
                "account":  m_account or "Other",
                "buy":      float(m_buy),
                "sell":     sell_price,
                "qty":      int(m_qty),
                "pnl":      pnl,
                "date":     str(m_date),
                "note":     m_note.strip(),
                "added":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            manual_entries.append(entry)
            _save_manual(manual_entries)
            st.success(f"✅ {m_symbol} entry saved!")
            st.rerun()

# ── show manual entries ───────────────────────────────────────────────────────
if manual_entries:
    _m_total_pnl = sum(e["pnl"] for e in manual_entries if e.get("pnl") is not None)
    _m_wins  = sum(1 for e in manual_entries if e.get("pnl") and e["pnl"] > 0)
    _m_loss  = sum(1 for e in manual_entries if e.get("pnl") and e["pnl"] < 0)
    _m_open  = sum(1 for e in manual_entries if not e.get("sell"))

    ms1, ms2, ms3, ms4 = st.columns(4)
    ms1.metric("Entries",    len(manual_entries))
    ms2.metric("Open",       _m_open)
    ms3.metric("Win / Loss", f"{_m_wins}W / {_m_loss}L")
    ms4.metric("Total P&L",  f"₹{_m_total_pnl:+,.0f}",
               delta="manual trades only")

    st.markdown("")
    for i, e in enumerate(reversed(manual_entries)):
        _idx   = len(manual_entries) - 1 - i
        _pnl   = e.get("pnl")
        _open  = e.get("sell") is None
        _border = "#2196f3" if _open else ("#4caf50" if _pnl and _pnl > 0 else ("#f44336" if _pnl and _pnl < 0 else "#9e9e9e"))
        _bg     = "#e3f2fd" if _open else ("#f1f8f1" if _pnl and _pnl > 0 else ("#fff1f1" if _pnl and _pnl < 0 else "#fafafa"))
        _status = "OPEN" if _open else ("WIN" if _pnl and _pnl > 0 else ("LOSS" if _pnl and _pnl < 0 else "CLOSED"))
        _pnl_str = f"<b style='color:{_border}'>₹{_pnl:+,.2f}</b>" if _pnl is not None else ""
        _sell_str = f"Sell: <b>₹{e['sell']:.2f}</b> · " if e.get("sell") else ""

        st.markdown(
            f"<div style='border:2px solid {_border};border-radius:10px;"
            f"padding:12px 16px;margin:4px 0;background:{_bg}'>"
            f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap'>"
            f"<span style='font-size:17px;font-weight:700'>{e['symbol']}</span>"
            f"<span style='color:#666;font-size:12px'>{e['account']}</span>"
            f"<span style='background:{_border};color:white;border-radius:4px;padding:1px 7px;font-size:12px'>{_status}</span>"
            f"</div>"
            f"<div style='margin-top:5px;font-size:13px;color:#333;display:flex;flex-wrap:wrap;gap:14px'>"
            f"<span>Qty: <b>{e['qty']}</b></span>"
            f"<span>Buy: <b>₹{e['buy']:.2f}</b></span>"
            f"{_sell_str}"
            f"<span>{e['date']}</span>"
            f"{_pnl_str}"
            f"</div>"
            + (f"<div style='margin-top:6px;font-size:12px;color:#555;border-left:3px solid {_border};"
               f"padding-left:8px'>{e['note']}</div>" if e.get("note") else "")
            + "</div>",
            unsafe_allow_html=True,
        )

        # edit / delete buttons
        eb1, eb2, eb3 = st.columns([2, 1, 1])
        with eb2:
            if st.button("✏️ Edit", key=f"m_edit_{_idx}", use_container_width=True):
                st.session_state[f"m_editing_{_idx}"] = True
                st.rerun()
        with eb3:
            if st.session_state.get(f"m_del_confirm_{_idx}"):
                dc1, dc2 = st.columns(2)
                if dc1.button("✓ Yes", key=f"m_del_yes_{_idx}", use_container_width=True):
                    manual_entries.pop(_idx)
                    _save_manual(manual_entries)
                    st.session_state.pop(f"m_del_confirm_{_idx}", None)
                    st.rerun()
                if dc2.button("✗ No", key=f"m_del_no_{_idx}", use_container_width=True):
                    st.session_state.pop(f"m_del_confirm_{_idx}", None)
                    st.rerun()
            else:
                if st.button("🗑 Delete", key=f"m_del_{_idx}", use_container_width=True):
                    st.session_state[f"m_del_confirm_{_idx}"] = True
                    st.rerun()

        # inline edit form
        if st.session_state.get(f"m_editing_{_idx}"):
            with st.form(key=f"m_edit_form_{_idx}"):
                ec1, ec2, ec3 = st.columns(3)
                with ec1:
                    e_sym  = st.text_input("Symbol", value=e["symbol"], key=f"e_sym_{_idx}")
                    e_acc  = st.text_input("Account", value=e["account"], key=f"e_acc_{_idx}")
                with ec2:
                    e_buy  = st.number_input("Buy ₹", value=float(e["buy"]), step=0.05, key=f"e_buy_{_idx}", format="%.2f")
                    e_sell = st.number_input("Sell ₹ (0=open)", value=float(e["sell"] or 0), step=0.05, key=f"e_sell_{_idx}", format="%.2f")
                with ec3:
                    e_qty  = st.number_input("Qty", value=int(e["qty"]), min_value=1, key=f"e_qty_{_idx}")
                e_note = st.text_area("Notes", value=e.get("note", ""), height=70, key=f"e_note_{_idx}")
                ef1, ef2 = st.columns(2)
                if ef1.form_submit_button("💾 Update", type="primary", use_container_width=True):
                    _sell = float(e_sell) if e_sell > 0 else None
                    _pnl  = round((_sell - float(e_buy)) * int(e_qty), 2) if _sell else None
                    manual_entries[_idx].update({
                        "symbol": e_sym.upper().strip(),
                        "account": e_acc,
                        "buy": float(e_buy),
                        "sell": _sell,
                        "qty": int(e_qty),
                        "pnl": _pnl,
                        "note": e_note.strip(),
                        "added": e.get("added", ""),
                    })
                    _save_manual(manual_entries)
                    st.session_state.pop(f"m_editing_{_idx}", None)
                    st.rerun()
                if ef2.form_submit_button("✗ Cancel", use_container_width=True):
                    st.session_state.pop(f"m_editing_{_idx}", None)
                    st.rerun()
else:
    st.info("No manual entries yet. Click **➕ Add New Manual Trade** above.")

if not df.empty:
    # ── export section ────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📥 Export")
    exp1, exp2 = st.columns(2)

    export_df = fdf[["symbol", "name", "status", "paper", "qty",
                      "buy_price", "sell_price", "pnl", "pnl_pct",
                      "buy_time", "sell_time", "note"]].copy()
    export_df.columns = ["Symbol", "Name", "Status", "Paper", "Qty",
                         "Buy ₹", "Sell ₹", "P&L ₹", "P&L %",
                         "Buy Time", "Sell Time", "Journal Note"]

    with exp1:
        st.download_button(
            "⬇️ Download as CSV",
            data=export_df.to_csv(index=False),
            file_name=f"trade_journal_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with exp2:
        st.download_button(
            "⬇️ Download as JSON",
            data=json.dumps(positions, indent=2, default=str),
            file_name=f"trade_history_{datetime.now().strftime('%Y%m%d')}.json",
            mime="application/json",
            use_container_width=True,
        )
