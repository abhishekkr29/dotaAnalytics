"""History page — browse cached matches you played in."""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from app import analyze as analyze_mod
from app import db
from app.web_auth import render_sidebar, require_login

st.set_page_config(page_title="History · dotaAnalytics", page_icon="📚", layout="wide")

aid = require_login()
render_sidebar(aid)

st.title("Match history")
st.caption("Matches you've played that are cached locally. Click a row to load it in Analyzer.")

with db.connect() as conn:
    rows = conn.execute(
        """
        SELECT m.match_id, m.start_time, m.duration, m.radiant_win, m.parsed,
               um.slot, um.hero_id, um.kills, um.deaths, um.assists
        FROM user_matches um
        JOIN matches m USING (match_id)
        WHERE um.user_id = %s
        ORDER BY m.start_time DESC
        """,
        (aid,),
    ).fetchall()

if not rows:
    st.info(
        "No cached matches for you yet. Use the CLI to bulk-fetch:\n\n"
        f"```bash\ndocker compose run --rm app python -m app.cli bracket-fetch --account {aid}\n```"
    )
    st.stop()

heroes = analyze_mod.heroes_by_id()

data = []
for r in rows:
    mid, st_time, dur, rw, parsed, slot, hero_id, k, d, a = r
    is_radiant = (slot or 0) < 128
    your_won = bool(rw) == is_radiant
    data.append({
        "match_id": mid,
        "date": datetime.fromtimestamp(st_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "hero": heroes.get(hero_id, {}).get("localized_name", "?"),
        "side": "Radiant" if is_radiant else "Dire",
        "result": "Win" if your_won else "Loss",
        "kda": f"{k}/{d}/{a}" if k is not None else "?",
        "duration_min": (dur or 0) // 60,
        "parsed": "yes" if parsed else "no",
    })
df = pd.DataFrame(data)

c1, c2, c3 = st.columns(3)
with c1:
    hero_filter = st.multiselect("Hero", sorted(df["hero"].unique()))
with c2:
    result_filter = st.multiselect("Result", ["Win", "Loss"])
with c3:
    parsed_only = st.checkbox("Parsed only", value=True)

filtered = df.copy()
if hero_filter:
    filtered = filtered[filtered["hero"].isin(hero_filter)]
if result_filter:
    filtered = filtered[filtered["result"].isin(result_filter)]
if parsed_only:
    filtered = filtered[filtered["parsed"] == "yes"]

st.caption(f"{len(filtered)} of {len(df)} matches")

selected = st.dataframe(
    filtered,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
)

if selected.selection.rows:
    chosen_idx = selected.selection.rows[0]
    chosen_match = int(filtered.iloc[chosen_idx]["match_id"])
    st.session_state["selected_match_id"] = chosen_match
    st.success(f"Loading match {chosen_match} in Analyzer...")
    st.switch_page("pages/1_Analyzer.py")
