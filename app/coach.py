import json
import time
from collections import Counter

import anthropic

from app import analyze as analyze_mod
from app import config, fetcher

MEMORY_LIMIT = 20  # how many entries to keep in coach_memory.json
MEMORY_IN_PROMPT = 5  # how many recent matches to inject into the next prompt

MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

SYSTEM_PROMPT = """You are a Dota 2 Turbo coach reviewing a match for a single player. Speak as a high-MMR friend talking through the replay — direct, constructive, specific.

You receive structured findings from a win-probability model PLUS raw match context:
- All 10 hero profiles (KDA, GPM, XPM, net worth, lane role, key item timings)
- Smoke usage timeline for both teams
- Per-minute win-probability curve (your team's perspective)
- Per-minute kill counts for each side
- Decision events scored by Δ win-prob

Use this raw data to make concrete tactical points. The model has *already* labelled the leak/kept events; your job is to add the **causal explanation and counterfactuals** that the model can't.

Your response must be markdown with these sections in order:

1. **Opening** (1-2 sentences): hero, result, framing.

2. **Phase-by-phase review** (2-4 short paragraphs): early (≤8 min), mid (8-15 min), late (>15 min). Skip any phase without notable events. Be specific — reference enemy heroes **by name**, their builds, their KDAs, and the win-prob curve at key moments. Don't just describe events; explain *why* they happened.

3. **What could have been done differently** (REQUIRED for losses; optional for wins): identify 2-3 concrete counterfactual moments. Each must cite specific raw data. Cover at least three dimensions across them:
   - **Item-build counters**: was an item bought too late vs an enemy's timing? Was the wrong item picked? Suggest specific alternatives with reasoning (e.g., "Vessel over Pipe because the enemy lineup is more physical than magical").
   - **Farm pattern**: read the per-player `farm: min5/min10/min15` snapshots. If the player's gold/lh at minute 10 is below average for their lane role, identify *where* they should have farmed instead (safe jungle camps after a kill, lane creep waves while supports rotate, etc.). Spot enemies whose `farm` is suspiciously high relative to kills involving them — that's a hero farming alone, gankable.
   - **Timing windows**: enemy carry's BKB not online yet → fight window. Smoke event by your team → which kill should it have enabled. Roshan timing. Tower-killing windows.
   Reference items actually built, smoke events that actually happened, win-prob numbers, and farm snapshots.

4. **Item-build prescription**: a short list of 2-4 items the player should have built or built differently, with reasoning. Reference the actual enemy lineup and your hero's role. Example: "Mek over Pipe in this game — your team has 3 squishies and the enemy mass-AoE damage isn't from one big nuke, it's from sustained Enchantress + Ogre right-clicks." Skip this section entirely if the build was correct.

5. **Three takeaways** (numbered list): specific, actionable for next time. Tied to what you played, who you played against, and the data above.

Rules:
- Only reference events listed in the findings or raw data. Do not invent events, stats, items, ward positions, or facts you can't see in the provided context.
- Cite timestamps in MM:SS format when relevant.
- Cite win-prob deltas only when meaningful (|Δ| ≥ 5%).
- Reference specific enemy heroes by name when giving advice — never "the enemy carry" if you can say "Phantom Assassin".
- For every item / build suggestion, name the specific *reason it counters this opponent in this game* — don't generic-recommend BKB without identifying the threat it cancels.
- For farm-pattern advice, cite the actual `farm: minX:Yg/Zlh/Wxp` numbers; "you under-farmed" is too vague, "you had 1800g vs the enemy mid's 2900g at minute 10 — that's a 60% deficit" is what we want.
- Aim for 500-800 words. Counterfactuals + item prescription mean longer is fine; padding still isn't.
- Tone: honest, constructive, direct. Don't sugarcoat losses but don't pile on either.
- Don't moralize ("you played poorly"). Explain mechanically what went wrong and what specifically to do instead.
- If a "Recent match history" section is supplied, look for recurring patterns (same hero killing them repeatedly across matches, same itemization mistake, same hero played) and call them out by name.
"""


def _phase_of(t_str: str) -> str:
    t_min = int(t_str.split(":")[0])
    return "early" if t_min < 8 else ("mid" if t_min < 15 else "late")


_LANE_ROLE = {1: "safe", 2: "mid", 3: "off", 4: "jungle"}


def _mmss(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _farm_snapshots(p: dict, minutes=(5, 10, 15)) -> list[str]:
    """Compact "min:gold/lh/xp" markers — lets the coach spot farm patterns."""
    gold_t = p.get("gold_t") or []
    xp_t = p.get("xp_t") or []
    lh_t = p.get("lh_t") or []
    snaps = []
    for m in minutes:
        if m < min(len(gold_t), len(lh_t), len(xp_t)):
            snaps.append(f"min{m}:{gold_t[m]}g/{lh_t[m]}lh/{xp_t[m]}xp")
    return snaps


def _player_profile(p: dict, heroes: dict) -> dict:
    hero = heroes.get(p.get("hero_id"), {}).get("localized_name", "?")
    items: list[str] = []
    for item in (p.get("purchase_log") or []):
        key = item.get("key", "")
        if key in analyze_mod.KEY_ITEMS:
            items.append(f"{analyze_mod.KEY_ITEMS[key]}@{_mmss(item['time'])}")
    return {
        "hero": hero,
        "kda": f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)}",
        "gpm": p.get("gold_per_min"),
        "xpm": p.get("xp_per_min"),
        "net_worth": p.get("net_worth"),
        "lane_role": _LANE_ROLE.get(p.get("lane_role")) or "?",
        "key_items": items[:8],
        "farm_snapshots": _farm_snapshots(p),
    }


def _smoke_events(match: dict, heroes: dict, your_team_radiant: bool) -> list[str]:
    events: list[tuple[int, str]] = []
    for p in match.get("players") or []:
        is_radiant = (p.get("player_slot") or 0) < 128
        side = "your team" if is_radiant == your_team_radiant else "enemy"
        hero = heroes.get(p.get("hero_id"), {}).get("localized_name", "?")
        for item in (p.get("purchase_log") or []):
            if item.get("key") == "smoke_of_deceit":
                t = item["time"]
                events.append((t, f"{_mmss(t)} — {hero} ({side})"))
    events.sort()
    return [e[1] for e in events]


def _kill_timeline(match: dict) -> str:
    """Compact per-minute kill counts: "min N: R<r>/D<d>; ..." (skipping zero-kill minutes)."""
    dur_min = (match.get("duration") or 0) // 60 + 1
    radiant = [0] * dur_min
    dire = [0] * dur_min
    for p in match.get("players") or []:
        is_radiant = (p.get("player_slot") or 0) < 128
        for k in (p.get("kills_log") or []):
            t = (k.get("time") or 0) // 60
            if 0 <= t < dur_min:
                if is_radiant:
                    radiant[t] += 1
                else:
                    dire[t] += 1
    chunks = [f"{i:02d}: R{radiant[i]}/D{dire[i]}" for i in range(dur_min)
              if radiant[i] or dire[i]]
    return "; ".join(chunks) or "(no kills logged)"


def _comeback_note(curve: list[float], won: bool) -> str:
    """One-line note about the latest winnable moment (loss) or closest-to-losing dip (win)."""
    if not curve or len(curve) < 3:
        return ""
    if won:
        lowest_i = min(range(len(curve)), key=lambda i: curve[i])
        if curve[lowest_i] >= 0.35:
            return ""
        return (
            f"Closest you got to losing: minute {lowest_i:02d}, "
            f"win-prob dipped to {curve[lowest_i]:.0%}."
        )
    # loss: latest minute with win-prob >= 30%
    winnable_until = -1
    for i, p in enumerate(curve):
        if p >= 0.30:
            winnable_until = i
    if winnable_until < 0 or winnable_until >= len(curve) - 2:
        return ""
    return (
        f"Last realistically winnable moment: minute {winnable_until:02d} "
        f"(win-prob still {curve[winnable_until]:.0%}). "
        f"Curve collapsed after that."
    )


def _build_beats(report: dict, match: dict) -> dict:
    you = report["you"]
    decisions = report["decisions"]
    all_decisions = decisions["biggest_leaks"] + decisions["kept_doing_this"]

    by_phase = {"early": [], "mid": [], "late": []}
    for d in all_decisions:
        by_phase[_phase_of(d["t"])].append(d)

    patterns = []
    deaths = [d for d in all_decisions if d["type"] == "death"]
    killer_counts = Counter(d["detail"].replace("Died to ", "") for d in deaths)
    for name, count in killer_counts.items():
        if count >= 2:
            patterns.append(f"Killed {count} times by {name}.")

    curve = report["win_prob_curve"]
    if curve and len(curve) >= 2:
        peak_i = max(range(len(curve)), key=lambda i: curve[i])
        trough_i = min(range(len(curve)), key=lambda i: curve[i])
        patterns.append(
            f"Win-prob peaked at {curve[peak_i]:.0%} around min {peak_i:02d}, "
            f"bottomed at {curve[trough_i]:.0%} around min {trough_i:02d}."
        )

    heroes = analyze_mod.heroes_by_id()
    you_account = config.require_account_id()
    you_player = fetcher.your_player(match)
    your_team_radiant = (you_player.get("player_slot") or 0) < 128

    teammates: list[dict] = []
    enemies: list[dict] = []
    for p in match.get("players") or []:
        if p.get("account_id") == you_account:
            continue
        prof = _player_profile(p, heroes)
        is_radiant = (p.get("player_slot") or 0) < 128
        (teammates if is_radiant == your_team_radiant else enemies).append(prof)

    return {
        "summary": {
            **you,
            "duration_min": report["duration_min"],
            "match_id": report["match_id"],
            "patch": match.get("patch"),
            "avg_rank_tier": fetcher.avg_rank_tier(match),
        },
        "by_phase": by_phase,
        "patterns": patterns,
        "teammates": teammates,
        "enemies": enemies,
        "smoke_events": _smoke_events(match, heroes, your_team_radiant),
        "kill_timeline": _kill_timeline(match),
        "win_prob_curve": curve,
        "comeback_note": _comeback_note(curve, you["result"] == "win"),
    }


def _format_decision_lines(items: list) -> list[str]:
    if not items:
        return ["  (no notable events in this phase)"]
    lines = []
    for d in items:
        sign = "+" if d["impact"] >= 0 else ""
        lines.append(
            f"  - {d['t']} | {d['type']:<7} | Δwp {sign}{d['impact']:.1%} | {d['detail']}"
        )
    return lines


def _format_profile_line(prof: dict) -> str:
    items = ", ".join(prof["key_items"]) if prof["key_items"] else "(no key items)"
    farm = " ".join(prof["farm_snapshots"]) or "(no farm snaps)"
    return (
        f"  - {prof['hero']:<20} KDA {prof['kda']:<10} "
        f"GPM {prof['gpm']:<5} XPM {prof['xpm']:<5} "
        f"NW {prof['net_worth']:<6} lane {prof['lane_role']:<6}\n"
        f"    farm: {farm}\n"
        f"    items: {items}"
    )


def _build_user_prompt(beats: dict) -> str:
    s = beats["summary"]
    lines = [
        f"## Match {s['match_id']}",
        f"- You: **{s['hero']}** on **{s['team']}**, KDA **{s['kda']}**",
        f"- Result: **{s['result'].upper()}**",
        f"- Duration: {s['duration_min']} min  |  Avg rank tier: {s.get('avg_rank_tier')}  |  Patch: {s.get('patch')}",
    ]

    # Hero composition with full profiles
    lines.append("\n## Hero profiles (KDA / GPM / XPM / net worth / lane / key items with timings)")
    lines.append("\n### Your team (excluding you)")
    for prof in beats["teammates"]:
        lines.append(_format_profile_line(prof))
    lines.append("\n### Enemy team")
    for prof in beats["enemies"]:
        lines.append(_format_profile_line(prof))

    # Win-prob curve (compact per-minute)
    curve = beats.get("win_prob_curve") or []
    if curve:
        curve_str = ", ".join(f"{i:02d}:{p:.0%}" for i, p in enumerate(curve))
        lines.append(f"\n## Win-prob curve (your perspective, per minute)\n{curve_str}")

    # Kill timeline
    lines.append(f"\n## Kill timeline (R = Radiant kills, D = Dire kills per minute)\n{beats['kill_timeline']}")

    # Smoke events
    if beats["smoke_events"]:
        lines.append("\n## Smoke events (gank initiation attempts)")
        for ev in beats["smoke_events"]:
            lines.append(f"- {ev}")
    else:
        lines.append("\n## Smoke events\n- (no smokes bought by either team)")

    # Scored decisions by phase
    lines.append("\n## Scored decisions by phase (Δ win-prob already signed for your team)")
    for phase in ("early", "mid", "late"):
        lines.append(f"\n### {phase.capitalize()} game")
        lines.extend(_format_decision_lines(beats["by_phase"][phase]))

    if beats["patterns"]:
        lines.append("\n## Notable patterns")
        for p in beats["patterns"]:
            lines.append(f"- {p}")

    if beats.get("comeback_note"):
        lines.append(f"\n## Trajectory note\n- {beats['comeback_note']}")

    lines.append(
        "\nWrite the coach review now. Use the raw data above to be specific in the "
        "'what could have been done differently' and 'Item-build prescription' sections — "
        "reference enemy heroes by name, their actual items + timings, real farm snapshots "
        "(e.g. `min10:2400g/40lh`), and real win-prob numbers."
    )
    return "\n".join(lines)


def _load_memory() -> dict:
    """Load coach memory from disk, or initialize a fresh structure."""
    path = config.MEMORY_PATH
    if not path.exists():
        return {"account_id": config.require_account_id(), "history": []}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"account_id": config.require_account_id(), "history": []}


def _save_memory(mem: dict) -> None:
    config.MEMORY_PATH.write_text(json.dumps(mem, indent=2))


def _extract_themes(report: dict) -> list[str]:
    """Pull a few heuristic themes out of the analyze output for memory storage."""
    themes: list[str] = []
    leaks = report["decisions"]["biggest_leaks"]
    kept = report["decisions"]["kept_doing_this"]

    leak_types = Counter(d["type"] for d in leaks)
    for kind, _ in leak_types.most_common(2):
        themes.append(f"leak:{kind}")

    death_killers = Counter(
        d["detail"].replace("Died to ", "") for d in leaks if d["type"] == "death"
    )
    for name, count in death_killers.items():
        if count >= 2:
            themes.append(f"repeat-deaths:{name}")

    if kept:
        themes.append(f"kept:{kept[0]['type']}")

    return themes


def _format_memory_for_prompt(mem: dict) -> str:
    history = (mem.get("history") or [])[-MEMORY_IN_PROMPT:]
    if not history:
        return ""
    lines = ["", "## Recent match history (your last reviewed games, oldest → newest)"]
    for h in history:
        themes = ", ".join(h.get("themes", [])) or "(none)"
        lines.append(
            f"- {h.get('date', '?')} | {h.get('hero', '?')} | "
            f"{h.get('result', '?')} | KDA {h.get('kda', '?')} | themes: {themes}"
        )
    return "\n".join(lines)


def _append_to_memory(mem: dict, report: dict) -> None:
    mem.setdefault("history", []).append({
        "match_id": report["match_id"],
        "date": time.strftime("%Y-%m-%d"),
        "hero": report["you"]["hero"],
        "result": report["you"]["result"],
        "kda": report["you"]["kda"],
        "themes": _extract_themes(report),
    })
    mem["history"] = mem["history"][-MEMORY_LIMIT:]


def _require_api_key() -> None:
    if not (config.ANTHROPIC_API_KEY or "").strip():
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Add it to .env "
            "(the file is gitignored, so the key stays out of git)."
        )


def coach(match_id: int, model: str = "sonnet", top_k: int = 6, min_impact: float = 0.005) -> dict:
    _require_api_key()

    report = analyze_mod.analyze(match_id, top_k=top_k, min_impact=min_impact)
    match = fetcher.fetch_match(match_id)
    beats = _build_beats(report, match)

    memory = _load_memory()
    user_prompt = _build_user_prompt(beats) + _format_memory_for_prompt(memory)

    model_id = MODEL_ALIASES.get(model, model)
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=model_id,
            max_tokens=3500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            cache_control={"type": "ephemeral"},
        )
    except anthropic.AuthenticationError as e:
        raise SystemExit(f"Anthropic authentication failed — check ANTHROPIC_API_KEY in .env. ({e.message})")
    except anthropic.RateLimitError as e:
        raise SystemExit(f"Rate limited by Anthropic API: {e.message}")
    except anthropic.APIStatusError as e:
        raise SystemExit(f"Anthropic API error ({e.status_code}): {e.message}")

    markdown = next((b.text for b in resp.content if b.type == "text"), "")

    out_path = config.REVIEWS_DIR / f"{match_id}.md"
    out_path.write_text(markdown)

    _append_to_memory(memory, report)
    _save_memory(memory)

    return {
        "match_id": match_id,
        "model": model_id,
        "review_path": str(out_path),
        "memory_entries": len(memory["history"]),
        "usage": {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        },
    }
