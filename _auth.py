"""
Session-scoped login guard — survives page refreshes, resets on browser close.

Usage in every page (after st.set_page_config):
    from _auth import require_login
    require_login()

Behaviour:
- On login, sets a browser cookie valid for 12 hours (one trading day).
- Refreshing the page reads the cookie and skips the login form.
- Closing the browser and reopening after the cookie expires shows login again.
- Credentials come from .env (RAMA_USERNAME / RAMA_PASSWORD).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
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
_COOKIE_NAME = "rama_auth"
_COOKIE_HOURS = 12   # re-login after 12 h of inactivity (covers a full trading day)


@st.cache_resource
def _cookie_manager():
    try:
        import extra_streamlit_components as stx
        return stx.CookieManager(key="_rama_cookie_mgr")
    except Exception:
        return None


def _read_cookie() -> bool:
    cm = _cookie_manager()
    if cm is None:
        return False
    try:
        return cm.get(_COOKIE_NAME) == "1"
    except Exception:
        return False


def _set_cookie() -> None:
    cm = _cookie_manager()
    if cm is None:
        return
    try:
        cm.set(
            _COOKIE_NAME,
            "1",
            expires_at=datetime.now() + timedelta(hours=_COOKIE_HOURS),
            key="_set_rama_auth",
        )
    except Exception:
        pass


def require_login() -> None:
    """Block page rendering until the user authenticates."""
    # Restore from cookie (survives F5 / tab refresh)
    if not st.session_state.get("authenticated") and _read_cookie():
        st.session_state["authenticated"] = True

    if st.session_state.get("authenticated"):
        return

    # ── login UI ──────────────────────────────────────────────────────────────
    st.markdown("""
    <style>
    section[data-testid="stSidebar"]  { display: none !important; }
    div[data-testid="stHeader"]        { display: none !important; }
    div[data-testid="stToolbar"]       { display: none !important; }
    .block-container { max-width: 420px !important; padding-top: 80px !important; }
    .login-logo  { text-align:center; font-size:36px; margin-bottom:6px; }
    .login-title {
        text-align:center; font-size:22px; font-weight:800;
        color:#1a73e8; letter-spacing:2px; margin-bottom:2px;
    }
    .login-sub   { text-align:center; font-size:13px; color:#888; margin-bottom:24px; }
    div[data-testid="stForm"] {
        border:1px solid #e0e0e0; border-radius:16px;
        padding:28px 32px !important; background:#fff;
        box-shadow:0 4px 16px rgba(0,0,0,0.08);
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
                _set_cookie()
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Invalid username or password. Please try again.")

    st.stop()
