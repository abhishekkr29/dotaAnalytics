"""Patterns — visualize what your coach memory has accumulated."""

import json
from collections import Counter

import pandas as pd
import streamlit as st

from app import config
from app.web_auth import render_sidebar, require_login

st.set_page_config(page_title="Patterns · dotaAnalytics", page_icon="📊", layout="wide")

aid = require_login()
render_sidebar(aid)

st.title("Patterns across your reviewed games")

mem_path = config.memory_path(aid)
if not mem_path.exists():
    st.info("No coach memory yet — generate a coach review on a parsed match first.")
    st.stop()

mem = json.loads(mem_path.read_text())
history = mem.get("history", [])
if not history:
    st.info("Coach memory is empty.")
    st.stop()

st.caption(f"Memory of last **{len(history)}** reviewed matches.")

c1, c2, c3 = st.columns(3)
wins = sum(1 for h in history if h.get("result") == "win")
losses = sum(1 for h in history if h.get("result") == "loss")
c1.metric("Reviewed", len(history))
c2.metric("Wins", wins)
c3.metric("Losses", losses)

st.divider()
st.subheader("Heroes played (most reviewed)")
hero_counts = Counter(h["hero"] for h in history)
hdf = pd.DataFrame(hero_counts.most_common(), columns=["hero", "games"])
st.bar_chart(hdf.set_index("hero"), height=240)

st.divider()
st.subheader("Recurring themes (across reviews)")
all_themes: Counter = Counter()
for h in history:
    for t in h.get("themes", []):
        all_themes[t] += 1
if all_themes:
    tdf = pd.DataFrame(all_themes.most_common(20), columns=["theme", "count"])
    st.dataframe(tdf, use_container_width=True, hide_index=True)
else:
    st.caption("(no themes detected yet)")

st.divider()
st.subheader("Repeat-deaths to specific enemies")
death_themes = [t for h in history for t in h.get("themes", []) if t.startswith("repeat-deaths:")]
if death_themes:
    dc = Counter(t.split(":", 1)[1] for t in death_themes)
    ddf = pd.DataFrame(dc.most_common(), columns=["enemy hero", "matches"])
    st.dataframe(ddf, use_container_width=True, hide_index=True)
else:
    st.caption("No repeated death patterns surfaced yet.")
