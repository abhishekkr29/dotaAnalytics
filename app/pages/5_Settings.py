"""Settings — BYO Anthropic key, usage stats, sign-out."""

import anthropic
import streamlit as st

from app import cost, crypto, db
from app.web_auth import display_name, logout, render_sidebar, require_login

st.set_page_config(page_title="Settings · dotaAnalytics", page_icon="⚙️", layout="wide")

aid = require_login()
render_sidebar(aid)

st.title("Settings")

with db.connect() as conn:
    row = conn.execute(
        "SELECT (anthropic_key_encrypted IS NOT NULL), steam_id_64, last_seen_at "
        "FROM users WHERE account_id = %s",
        (aid,),
    ).fetchone()
has_byo, steam_id, last_seen = (row or (False, None, None))

c1, c2, c3 = st.columns(3)
c1.metric("Player", display_name(aid))
c2.metric("Account ID", aid)
c3.metric("Steam ID 64", steam_id if steam_id else "—")

st.divider()
st.subheader("Anthropic API key (BYO)")
st.caption(
    "Server's shared key is used by default, with a daily cap on per-user spend. "
    "Paste your own key to bypass the cap — Anthropic bills you directly for those calls. "
    "Stored encrypted at rest with Fernet."
)

if has_byo:
    st.success("BYO key is active — daily cap is bypassed.")
    if st.button("Remove my BYO key"):
        with db.connect() as conn:
            conn.execute(
                "UPDATE users SET anthropic_key_encrypted = NULL WHERE account_id = %s",
                (aid,),
            )
        st.success("Removed. Server key + daily cap will apply on the next coach review.")
        st.rerun()
else:
    st.info("Using the server's default key (subject to the daily cap).")

new_key = st.text_input(
    "Paste a new Anthropic API key (sk-ant-...)",
    type="password",
    placeholder="sk-ant-api03-...",
)
if st.button("Validate & save", disabled=not new_key):
    try:
        client = anthropic.Anthropic(api_key=new_key.strip())
        client.models.list()
    except anthropic.AuthenticationError:
        st.error("Anthropic rejected that key — check it and try again.")
    except Exception as e:
        st.error(f"Couldn't validate key: {e}")
    else:
        ciphertext = crypto.encrypt_key(new_key.strip())
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO users (account_id, anthropic_key_encrypted)
                   VALUES (%s, %s)
                   ON CONFLICT (account_id) DO UPDATE SET
                       anthropic_key_encrypted = EXCLUDED.anthropic_key_encrypted""",
                (aid, ciphertext),
            )
        st.success("Saved. Cap bypassed for your future reviews.")
        st.rerun()

st.divider()
st.subheader("Usage")
usage = cost.current_usage(aid)
c1, c2, c3 = st.columns(3)
c1.metric("Today (server key)", f"{usage['daily_cents']}¢", f"of {usage['cap_cents']}¢ cap")
c2.metric("Month-to-date (all)", f"{usage['monthly_cents']}¢")
c3.metric("Last seen", str(last_seen) if last_seen else "—")

st.divider()
if st.button("Sign out", type="primary"):
    logout()
    st.switch_page("web.py")
