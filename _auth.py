"""
Session-scoped login guard.

Usage in every page (after st.set_page_config):
    from _auth import require_login
    require_login()

Behaviour:
- Logged-in flag lives in st.session_state["authenticated"].
- session_state resets when the browser tab is closed/refreshed → user must
  log in again on a new session, but NOT within the same open session.
- Credentials come from .env (RAMA_USERNAME / RAMA_PASSWORD).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

_USER = os.getenv("RAMA_USERNAME", "manideep")
_PASS = os.getenv("RAMA_PASSWORD", "28061991")


def require_login() -> None:
    """Block page rendering until the user authenticates this session."""
    if st.session_state.get("authenticated"):
        return

    # ── login UI ──────────────────────────────────────────────────────────────
    st.markdown("""
    <style>
    /* hide sidebar and header until logged in */
    section[data-testid="stSidebar"]  { display: none !important; }
    div[data-testid="stHeader"]        { display: none !important; }
    div[data-testid="stToolbar"]       { display: none !important; }
    .block-container { max-width: 420px !important; padding-top: 80px !important; }
    .login-logo {
        text-align: center; font-size: 36px; margin-bottom: 6px;
    }
    .login-title {
        text-align: center; font-size: 22px; font-weight: 800;
        color: #1a73e8; letter-spacing: 2px; margin-bottom: 2px;
    }
    .login-sub {
        text-align: center; font-size: 13px; color: #888; margin-bottom: 24px;
    }
    div[data-testid="stForm"] {
        border: 1px solid #e0e0e0; border-radius: 16px;
        padding: 28px 32px !important; background: #fff;
        box-shadow: 0 4px 16px rgba(0,0,0,0.08);
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("<div class='login-logo'>📈</div>", unsafe_allow_html=True)
    st.markdown("<div class='login-title'>RAMA · ManiMindsMoney</div>", unsafe_allow_html=True)
    st.markdown("<div class='login-sub'>NSE Intraday Trading Suite</div>", unsafe_allow_html=True)

    with st.form("_login_form", clear_on_submit=False):
        username = st.text_input("Username", placeholder="Enter username")
        password = st.text_input("Password", type="password", placeholder="Enter password")
        submitted = st.form_submit_button("Login", use_container_width=True, type="primary")

        if submitted:
            if username.strip() == _USER and password == _PASS:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Invalid username or password. Please try again.")

    st.stop()
