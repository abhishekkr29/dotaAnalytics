"""Home — Steam sign-in handoff + onboarding hint. Sidebar nav drops you into pages."""

import streamlit as st

from app import config, db
from app.web_auth import current_user, display_name, render_sidebar

st.set_page_config(page_title="dotaAnalytics", page_icon="🎯", layout="wide")

aid = current_user()

if aid is None:
    st.title("dotaAnalytics")
    st.caption("Personal Dota 2 Turbo decision analyzer.")
    st.markdown(
        f"### [Sign in with Steam]({config.AUTH_PUBLIC_URL}/auth/steam/login)\n\n"
        "We use Steam OpenID — you sign in on Steam's site, we only receive your Steam ID. "
        "After sign-in you'll be redirected back here, logged in."
    )
    st.stop()

render_sidebar(aid)

st.title(f"Welcome, {display_name(aid)}")
st.caption("Use the sidebar to navigate.")

# Onboarding — show the bracket-fetch hint if no matches cached yet.
with db.connect() as conn:
    n_user_matches = conn.execute(
        "SELECT COUNT(*) FROM user_matches WHERE user_id = %s", (aid,)
    ).fetchone()[0]
    n_parsed_user = conn.execute(
        """SELECT COUNT(*) FROM user_matches um
           JOIN matches m USING (match_id)
           WHERE um.user_id = %s AND m.parsed""",
        (aid,),
    ).fetchone()[0]

c1, c2, c3 = st.columns(3)
c1.metric("Your matches", n_user_matches)
c2.metric("Parsed", n_parsed_user)
c3.metric("Cache hit-rate", f"{(100 * n_parsed_user / max(1, n_user_matches)):.0f}%")

st.divider()
if n_user_matches == 0:
    st.warning("You don't have any cached matches yet. Two ways to get started:")
    st.markdown(
        f"**1. Just paste a match ID in the Analyzer** — it auto-fetches single matches "
        f"on demand from OpenDota.\n\n"
        f"**2. Bulk-fetch your bracket** (CLI, takes ~minutes):\n\n"
        f"```bash\n"
        f"docker compose run --rm app python -m app.cli profile        --account {aid}\n"
        f"docker compose run --rm app python -m app.cli bracket-fetch  --account {aid} --limit 500\n"
        f"docker compose run --rm app python -m app.cli request-parses --limit 500\n"
        f"# wait ~15 min, then:\n"
        f"docker compose run --rm app python -m app.cli refresh-parses --account {aid} --limit 500\n"
        f"```"
    )
else:
    st.success(f"You have **{n_user_matches}** matches on file. Head to **Analyzer** to review one.")
