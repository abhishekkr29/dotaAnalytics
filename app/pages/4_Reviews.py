"""Reviews — browse past coach reviews."""

from datetime import datetime

import streamlit as st

from app import config
from app.web_auth import display_name, render_sidebar, require_login

st.set_page_config(page_title="Reviews · dotaAnalytics", page_icon="📝", layout="wide")

aid = require_login()
render_sidebar(aid)

st.title("Coach reviews")

review_dir = config.reviews_dir_for(aid)
md_files = sorted(review_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

if not md_files:
    st.info("No coach reviews yet — generate one from the Analyzer page.")
    st.stop()

st.caption(f"{len(md_files)} reviews saved for **{display_name(aid)}**.")

def _label(p):
    when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"Match {p.stem}  ·  {when}"

sel = st.selectbox(
    "Pick a review",
    md_files,
    format_func=_label,
)

if sel:
    text = sel.read_text()
    st.markdown(text)
    st.download_button(
        "Download .md",
        data=text,
        file_name=sel.name,
        mime="text/markdown",
    )
