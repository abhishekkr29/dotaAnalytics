"""Match analyzer — paste a match ID, see win-prob + decisions + coach review."""

from pathlib import Path

import pandas as pd
import streamlit as st

from app import analyze as analyze_mod
from app import coach as coach_mod
from app import config, cost, fetcher
from app.web_auth import render_sidebar, require_login

st.set_page_config(page_title="Analyzer · dotaAnalytics", page_icon="🎯", layout="wide")

aid = require_login()
render_sidebar(aid)

st.title("Match analyzer")

default_match = str(st.session_state.pop("selected_match_id", "") or "")

c_in, c_btn = st.columns([4, 1])
with c_in:
    match_id_str = st.text_input(
        "Match ID",
        value=default_match,
        placeholder="e.g. 8807224804  (must be a Turbo match you played in)",
        label_visibility="collapsed",
    )
with c_btn:
    analyze_btn = st.button("Analyze", type="primary", use_container_width=True)

if analyze_btn and match_id_str:
    try:
        mid = int(match_id_str)
    except ValueError:
        st.error("Match ID must be an integer.")
        st.stop()

    cache_path = config.MATCHES_DIR / f"{mid}.json"
    if not cache_path.exists():
        with st.spinner(f"Fetching match {mid} from OpenDota..."):
            try:
                fetcher.sync_match(mid, account_id=aid)
            except Exception as e:
                st.error(f"Couldn't fetch match: {e}")
                st.stop()

    with st.spinner("Analyzing..."):
        try:
            report = analyze_mod.analyze(mid, account_id=aid)
        except SystemExit as e:
            st.error(str(e))
            st.stop()

    st.session_state.report = report
    st.session_state.match_id = mid
    st.session_state.pop("review", None)
    st.session_state.pop("review_usage", None)


if "report" in st.session_state and st.session_state.get("match_id"):
    report = st.session_state.report
    you = report["you"]

    badge = ":green[**WIN**]" if you["result"] == "win" else ":red[**LOSS**]"
    st.markdown(
        f"### {you['hero']} · {you['team']} · KDA **{you['kda']}** · "
        f"{badge} · {report['duration_min']} min"
    )

    curve_df = pd.DataFrame({"your win-prob": report["win_prob_curve"]})
    curve_df.index.name = "minute"
    st.line_chart(curve_df, height=240)

    c_leaks, c_kept = st.columns(2)
    with c_leaks:
        leaks = report["decisions"]["biggest_leaks"]
        st.subheader(f"Biggest leaks ({len(leaks)})")
        if not leaks:
            st.caption("(none above threshold — clean game)")
        for d in leaks:
            with st.container(border=True):
                hdr, body = st.columns([1, 3])
                hdr.markdown(f"**{d['t']}**")
                hdr.caption(d["type"])
                body.markdown(d["detail"])
                body.markdown(f":red[**{d['impact']:+.1%}**]")
    with c_kept:
        kept = report["decisions"]["kept_doing_this"]
        st.subheader(f"Kept doing this ({len(kept)})")
        if not kept:
            st.caption("(none above threshold)")
        for d in kept:
            with st.container(border=True):
                hdr, body = st.columns([1, 3])
                hdr.markdown(f"**{d['t']}**")
                hdr.caption(d["type"])
                body.markdown(d["detail"])
                body.markdown(f":green[**+{d['impact']:.1%}**]")

    st.divider()
    st.subheader("Coach review")

    coach_model = st.session_state.get("coach_model", "sonnet")
    coach_btn = st.button(f"Generate coach review · {coach_model}")

    if coach_btn:
        est = {"haiku": "1–2", "sonnet": "3–6", "opus": "6–12"}[coach_model]
        placeholder = st.empty()
        buf: list[str] = []

        def _on_chunk(text: str) -> None:
            buf.append(text)
            placeholder.markdown("".join(buf))

        with st.spinner(f"Streaming from {coach_model} (~{est}s)..."):
            try:
                result = coach_mod.coach(
                    st.session_state.match_id, account_id=aid, model=coach_model,
                    on_chunk=_on_chunk,
                )
                st.session_state.review = Path(result["review_path"]).read_text()
                st.session_state.review_usage = result["usage"]
                st.session_state.review_cost_cents = result.get("cost_cents", 0)
            except cost.BudgetExceeded as e:
                st.error(str(e))
            except SystemExit as e:
                st.error(str(e))

    if "review" in st.session_state:
        st.markdown(st.session_state.review)
        with st.expander("Token usage / cost"):
            st.json({
                **st.session_state.review_usage,
                "cents_charged": st.session_state.get("review_cost_cents", 0),
            })
