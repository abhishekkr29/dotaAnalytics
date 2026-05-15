"""Streamlit MVP — single-page match analyzer.

Run: `docker compose up -d web` → http://localhost:8501

Reuses `analyze`, `coach`, and `fetcher` directly. CLI workflows are untouched.
"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app import analyze as analyze_mod
from app import coach as coach_mod
from app import config, db, fetcher

st.set_page_config(page_title="dotaAnalytics", page_icon="🎯", layout="wide")


# ──────────────────────────────────────────────────────────────────────
# Sidebar — status snapshot
# ──────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("dotaAnalytics")
    st.caption("Personal Dota 2 Turbo coach")

    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FILTER (WHERE parsed) AS parsed, "
                "COUNT(*) AS total FROM matches"
            ).fetchone()
        parsed_count, total_count = row
    except Exception:
        parsed_count, total_count = 0, 0

    c1, c2 = st.columns(2)
    c1.metric("Parsed", f"{parsed_count:,}")
    c2.metric("Total", f"{total_count:,}")

    meta_path = config.DATA_DIR / "model_meta.json"
    if config.MODEL_PATH.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        st.metric("val_auc", f"{meta['val_auc']:.3f}")
        st.caption(f"on {meta['n_matches']:,} matches · {meta['n_rows']:,} rows")
    else:
        st.warning("No trained model — run `train` from CLI first")

    if config.MEMORY_PATH.exists():
        try:
            mem = json.loads(config.MEMORY_PATH.read_text())
            st.metric("Coach memory", len(mem.get("history", [])))
        except Exception:
            pass

    if config.ACCOUNT_ID:
        st.caption(f"Account: `{config.ACCOUNT_ID}`")

    st.divider()
    st.subheader("Coach model")
    coach_model = st.radio(
        "tier",
        ["haiku", "sonnet", "opus"],
        index=1,
        label_visibility="collapsed",
        format_func=lambda m: {
            "haiku":  "Haiku 4.5  · ~$0.001",
            "sonnet": "Sonnet 4.6 · ~$0.03",
            "opus":   "Opus 4.7   · ~$0.05",
        }[m],
    )

    if not (config.ANTHROPIC_API_KEY or "").strip():
        st.error("ANTHROPIC_API_KEY missing — coach disabled")


# ──────────────────────────────────────────────────────────────────────
# Main — analyzer
# ──────────────────────────────────────────────────────────────────────
st.title("Match analyzer")

c_in, c_btn = st.columns([4, 1])
with c_in:
    match_id_str = st.text_input(
        "Match ID",
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
                fetcher.sync_match(mid)
            except Exception as e:
                st.error(f"Couldn't fetch match: {e}")
                st.stop()

    with st.spinner("Analyzing..."):
        try:
            report = analyze_mod.analyze(mid)
        except SystemExit as e:
            st.error(str(e))
            st.stop()

    st.session_state.report = report
    st.session_state.match_id = mid
    # Coach review for previous match shouldn't carry over
    st.session_state.pop("review", None)
    st.session_state.pop("review_usage", None)


# ──────────────────────────────────────────────────────────────────────
# Render report
# ──────────────────────────────────────────────────────────────────────
if "report" in st.session_state:
    report = st.session_state.report
    you = report["you"]

    badge = ":green[**WIN**]" if you["result"] == "win" else ":red[**LOSS**]"
    st.markdown(
        f"### {you['hero']} · {you['team']} · KDA **{you['kda']}** · "
        f"{badge} · {report['duration_min']} min"
    )

    curve_df = pd.DataFrame({
        "your win-prob": report["win_prob_curve"],
    })
    curve_df.index.name = "minute"
    st.line_chart(curve_df, height=240)

    c_leaks, c_kept = st.columns(2)

    with c_leaks:
        leaks = report["decisions"]["biggest_leaks"]
        st.subheader(f"Biggest leaks ({len(leaks)})")
        if not leaks:
            st.caption("(none above threshold — a clean game from your perspective)")
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


    # Coach section
    st.divider()
    st.subheader("Coach review")

    if not (config.ANTHROPIC_API_KEY or "").strip():
        st.info("Set `ANTHROPIC_API_KEY` in `.env` to enable LLM coach reviews.")
    else:
        coach_btn = st.button(f"Generate coach review · {coach_model}")
        if coach_btn:
            est = {"haiku": "1–2", "sonnet": "3–6", "opus": "6–12"}[coach_model]
            with st.spinner(f"Generating with {coach_model} (~{est}s)..."):
                try:
                    result = coach_mod.coach(
                        st.session_state.match_id,
                        model=coach_model,
                    )
                    st.session_state.review = Path(result["review_path"]).read_text()
                    st.session_state.review_usage = result["usage"]
                except SystemExit as e:
                    st.error(str(e))

        if "review" in st.session_state:
            st.markdown(st.session_state.review)
            with st.expander("Token usage"):
                st.json(st.session_state.review_usage)
