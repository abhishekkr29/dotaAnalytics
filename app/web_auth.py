"""Shared auth + sidebar helpers for the Streamlit pages."""

import streamlit as st

from app import auth as auth_mod
from app import config, cost, db


def _ingest_token_param() -> None:
    """Pop ?token= from URL, validate it, populate session_state."""
    qp = st.query_params
    token = qp.get("token")
    if not token:
        return
    payload = auth_mod.verify_jwt(token)
    if payload and "account_id" in payload:
        st.session_state["account_id"] = int(payload["account_id"])
        st.session_state["auth_token"] = token
        st.session_state["auth_mode"] = "steam"
    qp.clear()


def _auto_login_dev() -> None:
    """If ACCOUNT_ID is set in .env and there's no session, log in as that account.

    Preserves the original single-user workflow when running locally — you don't
    have to wire Steam OpenID just to use your own deployment.
    """
    if "account_id" in st.session_state:
        return
    if config.ACCOUNT_ID:
        st.session_state["account_id"] = config.ACCOUNT_ID
        st.session_state["auth_mode"] = "dev-env"


def current_user() -> int | None:
    _ingest_token_param()
    _auto_login_dev()
    return st.session_state.get("account_id")


def logout() -> None:
    for k in ("account_id", "auth_token", "auth_mode"):
        st.session_state.pop(k, None)


def require_login() -> int:
    """Top-of-page guard for pages that need auth."""
    aid = current_user()
    if aid is None:
        st.title("Please sign in")
        st.markdown(
            f"[Sign in with Steam]({config.AUTH_PUBLIC_URL}/auth/steam/login) "
            "— authenticates via Steam OpenID; we never see your password."
        )
        st.stop()
    return aid


def render_sidebar(active_account: int) -> None:
    """Shared sidebar: current user, cost usage, model picker, sign-out."""
    with st.sidebar:
        st.title("dotaAnalytics")
        st.caption(f"Account `{active_account}`")
        mode = st.session_state.get("auth_mode", "?")
        st.caption(f"Auth: {mode}")

        usage = cost.current_usage(active_account)
        cap = usage["cap_cents"]
        if cap > 0:
            pct = min(1.0, usage["daily_cents"] / cap)
            st.progress(pct, text=f"Today: {usage['daily_cents']}¢ / {cap}¢")
        st.caption(f"Month-to-date: {usage['monthly_cents']}¢")

        with db.connect() as conn:
            row = conn.execute(
                "SELECT (anthropic_key_encrypted IS NOT NULL) FROM users WHERE account_id=%s",
                (active_account,),
            ).fetchone()
        has_byo = bool(row and row[0])
        if has_byo:
            st.success("BYO key active — cap bypassed.")

        st.divider()
        st.subheader("Coach model")
        st.session_state.setdefault("coach_model", "sonnet")
        st.session_state["coach_model"] = st.radio(
            "tier",
            ["haiku", "sonnet", "opus"],
            index=["haiku", "sonnet", "opus"].index(st.session_state["coach_model"]),
            label_visibility="collapsed",
            format_func=lambda m: {
                "haiku":  "Haiku 4.5  · ~$0.001",
                "sonnet": "Sonnet 4.6 · ~$0.03",
                "opus":   "Opus 4.7   · ~$0.05",
            }[m],
        )

        st.divider()
        if st.button("Sign out", use_container_width=True):
            logout()
            st.rerun()
