"""Match analyzer — paste a match ID, see win-prob + decisions + coach review."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from app import analyze as analyze_mod
from app import coach as coach_mod
from app import config, cost, db, fetcher
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

def _parse_request_age_minutes(match_id: int) -> int | None:
    """How many minutes ago was a parse last requested for this match? None if never."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT parse_requested_at FROM matches WHERE match_id = %s",
            (match_id,),
        ).fetchone()
    if not row or not row[0]:
        return None
    age = datetime.now(timezone.utc) - row[0]
    return int(age.total_seconds() // 60)


def _render_unparsed_ui(mid: int) -> None:
    """Show parse-request controls when a match exists at OpenDota but isn't parsed."""
    st.warning(
        f"Match `{mid}` hasn't been parsed by OpenDota yet. "
        "Replay parsing is queued by OpenDota and typically takes **5–30 minutes** "
        "with a Premium key (longer on free tier)."
    )

    age = _parse_request_age_minutes(mid)

    # Persistent status pill — much harder to miss than a disabled button.
    if age is None:
        st.info("📋 **Status:** no parse request submitted for this match yet.")
    else:
        st.success(
            f"✅ **Parse already requested {age} min ago.** "
            "Click **Refresh** below to check if OpenDota has finished parsing it. "
            "(The Request button is disabled to avoid duplicate POSTs within 24h.)"
        )

    c_req, c_refresh, _ = st.columns([1, 1, 3])
    with c_req:
        cooldown_active = age is not None and age < 60 * 24
        if st.button(
            "Request parse" if not cooldown_active else f"Requested {age}m ago",
            disabled=cooldown_active,
            help=("Cooldown: this match was already parse-requested within the last 24h"
                  if cooldown_active else
                  "POST /request/{id} to OpenDota's parse queue"),
            use_container_width=True,
        ):
            try:
                fetcher.request_parse(mid)
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE matches SET parse_requested_at = NOW() WHERE match_id = %s",
                        (mid,),
                    )
                st.session_state[f"parse_requested_just_now_{mid}"] = True
                st.rerun()
            except Exception as e:
                st.error(f"Couldn't request parse: {e}")
    with c_refresh:
        if st.button("Refresh", help="Bypass cache; re-fetch from OpenDota to pick up parse",
                     use_container_width=True):
            try:
                with st.spinner("Re-fetching from OpenDota..."):
                    fetcher.sync_match(mid, account_id=aid, force=True)
                st.rerun()
            except Exception as e:
                st.error(f"Couldn't refresh: {e}")

    # One-time toast after a successful request (lives across the auto-rerun).
    if st.session_state.pop(f"parse_requested_just_now_{mid}", False):
        st.toast("✅ Parse request sent to OpenDota", icon="📨")


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

    # Cheap check: is the match parsed? If not, render the parse-request UI instead.
    try:
        match_dict = json.loads(cache_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        st.error(f"Match {mid} cache unreadable: {e}")
        st.stop()

    if not fetcher.is_parsed(match_dict):
        st.session_state.pop("report", None)
        st.session_state.match_id = mid  # remember for the refresh button
        _render_unparsed_ui(mid)
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
    st.session_state.pop("recommendations", None)
    st.session_state.pop("blame", None)


# If the user hit Refresh on the unparsed UI, re-check parse status for the
# last-pasted match without requiring a re-click of Analyze.
if not analyze_btn and "match_id" in st.session_state and "report" not in st.session_state:
    mid = st.session_state["match_id"]
    cache_path = config.MATCHES_DIR / f"{mid}.json"
    if cache_path.exists():
        try:
            match_dict = json.loads(cache_path.read_text())
            if not fetcher.is_parsed(match_dict):
                _render_unparsed_ui(mid)
        except (FileNotFoundError, json.JSONDecodeError):
            pass


if "report" in st.session_state and st.session_state.get("match_id"):
    report = st.session_state.report
    you = report["you"]

    badge = ":green[**WIN**]" if you["result"] == "win" else ":red[**LOSS**]"
    st.markdown(
        f"### {you['hero']} · {you['team']} · KDA **{you['kda']}** · "
        f"{badge} · {report['duration_min']} min"
    )

    # Blame button: works on both wins and losses.
    #   On a loss → blames your worst teammate (excluding you by default)
    #   On a win  → blames the enemy team's worst player (the one who lost it for THEM)
    blame_path = config.blame_dir_for(aid) / f"{st.session_state.match_id}__auto.json"
    if "blame" not in st.session_state and blame_path.exists():
        try:
            cached_blame = json.loads(blame_path.read_text())
            if cached_blame.get("blame_text"):
                st.session_state.blame = cached_blame
        except json.JSONDecodeError:
            pass

    blame_label = (
        "🎭 Find the scapegoat · ~$0.003"
        if you["result"] == "win"
        else "🎭 Assign blame · ~$0.003"
    )
    blame_help = (
        "You won — narrator points at the enemy team's worst player"
        if you["result"] == "win"
        else "Stanley-Parable narrator decides who lost this for your team"
    )

    c_blame_btn, _c_pad = st.columns([1, 3])
    with c_blame_btn:
        if "blame" not in st.session_state:
            if st.button(blame_label, help=blame_help):
                with st.spinner("The narrator considers the evidence..."):
                    try:
                        r = coach_mod.assign_blame(
                            st.session_state.match_id, account_id=aid, model="haiku",
                        )
                        st.session_state.blame = r
                        st.rerun()
                    except cost.BudgetExceeded as e:
                        st.error(str(e))
                    except SystemExit as e:
                        st.error(str(e))
        else:
            if st.button("🔄 Reroll blame"):
                blame_path.unlink(missing_ok=True)
                st.session_state.pop("blame", None)
                st.rerun()

    if "blame" in st.session_state:
        b = st.session_state.blame
        tgt = b["target"]
        team_note = (
            " *(yes, that's you)*" if tgt.get("is_you")
            else " *(enemy team)*" if tgt.get("team") == "enemy"
            else ""
        )
        role_display = {
            "carry": "Carry (Pos 1)", "mid": "Mid (Pos 2)", "off": "Offlane (Pos 3)",
            "sup4": "Soft Support (Pos 4)", "sup5": "Hard Support (Pos 5)",
        }.get(tgt.get("inferred_role"), "?")
        with st.expander(
            f"🎭 The narrator has chosen: **{tgt['hero']}** · "
            f"{role_display} · KDA {tgt['kda']} · GPM {tgt['gpm']}{team_note}",
            expanded=True,
        ):
            st.markdown(b["blame_text"])
            # Surface the role-normalized blame factors so the user sees why
            factors = tgt.get("blame_factors") or {}
            if factors:
                rows = []
                for name, f in factors.items():
                    z_pct = f["z"] * 100
                    arrow = "🔴" if z_pct > 25 else "🟡" if z_pct > 0 else "🟢"
                    rows.append(
                        f"| {arrow} {name.replace('_', ' ')} | {f['value']} | {f['baseline']} | {z_pct:+.0f}% |"
                    )
                st.markdown(
                    "\n".join([
                        "| | actual | role baseline | deviation |",
                        "|---|---:|---:|---:|",
                        *rows,
                    ])
                )
            st.caption(
                f"_Generated with {b['model']}_ · _{b['cost_cents']}¢_ · "
                f"_target: highest role-normalized composite blame score_"
            )

    curve_df = pd.DataFrame({"your win-prob": report["win_prob_curve"]})
    curve_df.index.name = "minute"
    st.line_chart(curve_df, height=240)

    # Load cached recommendations from disk if available (across page reloads)
    if "recommendations" not in st.session_state:
        rec_path = config.recommendations_dir_for(aid) / f"{st.session_state.match_id}.json"
        if rec_path.exists():
            try:
                cached = json.loads(rec_path.read_text())
                if cached.get("recommendations"):
                    st.session_state.recommendations = cached["recommendations"]
            except json.JSONDecodeError:
                pass

    c_leaks, c_kept = st.columns(2)
    with c_leaks:
        leaks = report["decisions"]["biggest_leaks"]
        st.subheader(f"Biggest leaks ({len(leaks)})")
        if not leaks:
            st.caption("(none above threshold — clean game)")

        recs = st.session_state.get("recommendations", {})
        if leaks and not recs:
            if st.button("💡 Get tactical recommendations · ~$0.005",
                         help="One Claude Haiku call enriches each leak with concrete tactical advice"):
                with st.spinner("Analyzing each leak..."):
                    try:
                        r = coach_mod.recommend_per_leak(
                            st.session_state.match_id, account_id=aid, model="haiku",
                        )
                        st.session_state.recommendations = r.get("recommendations", {})
                        st.rerun()
                    except cost.BudgetExceeded as e:
                        st.error(str(e))
                    except SystemExit as e:
                        st.error(str(e))

        for i, d in enumerate(leaks):
            with st.container(border=True):
                hdr, body = st.columns([1, 3])
                hdr.markdown(f"**{d['t']}**")
                hdr.caption(d["type"])
                body.markdown(d["detail"])
                body.markdown(f":red[**{d['impact']:+.1%}**]")
                rec = recs.get(str(i + 1))
                if rec:
                    st.markdown(f"💡 _{rec}_")
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
