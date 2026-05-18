import json
import time
from collections import Counter
from typing import Callable

import anthropic

from app import analyze as analyze_mod
from app import baselines as baselines_mod
from app import config, cost, crypto, db, fetcher

MEMORY_LIMIT = 20  # how many entries to keep in coach_memory/<account_id>.json
MEMORY_IN_PROMPT = 5  # how many recent matches to inject into the next prompt
MEMORY_SAME_HERO_LIMIT = 5  # how many same-hero games to surface separately
MEMORY_MATCHUP_THRESHOLD = 2  # repeat-deaths to enemy hero before flagging

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
- Item-timing vs **bracket median** baselines (where available)
- Cross-match memory: same-hero history + recurring matchup patterns

Use this raw data to make concrete tactical points. The model has *already* labelled the leak/kept events; your job is to add the **causal explanation and counterfactuals** that the model can't.

Your response must be markdown with these sections in order:

1. **Opening** (1-2 sentences): hero, result, framing.

2. **Phase-by-phase review** (2-4 short paragraphs): early (≤8 min), mid (8-15 min), late (>15 min). Skip any phase without notable events. Be specific — reference enemy heroes **by name**, their builds, their KDAs, and the win-prob curve at key moments. Don't just describe events; explain *why* they happened.

3. **What could have been done differently** (REQUIRED for losses; optional for wins): identify 2-3 concrete counterfactual moments. Each must cite specific raw data. Cover at least three dimensions across them:
   - **Item-build counters**: was an item bought too late vs an enemy's timing? If the `## Item timings vs bracket baseline` section flags an item as "late" or "early", cite the specific bracket median (e.g., "BKB at 17:30 — bracket median is 14:00 for Storm at Crusader, so 3:30 late"). Suggest specific alternatives with reasoning.
   - **Farm pattern**: read the per-player `farm: min5/min10/min15` snapshots. If the player's gold/lh at minute 10 is below average for their lane role, identify *where* they should have farmed instead.
   - **Timing windows**: enemy carry's BKB not online yet → fight window. Smoke event by your team → which kill should it have enabled.

4. **Item-build prescription**: 2-4 items to build or build differently, with reasoning. Skip if the build was correct.

5. **Three takeaways** (numbered list): specific, actionable for next time.

Rules:
- Only reference events listed in the findings or raw data. Do not invent.
- Cite timestamps in MM:SS format when relevant.
- Cite win-prob deltas only when meaningful (|Δ| ≥ 5%).
- Reference specific enemy heroes by name when giving advice.
- For every item suggestion, name the specific *reason it counters this opponent in this game*.
- For farm-pattern advice, cite actual `farm: minX:Yg/Zlh/Wxp` numbers.
- Aim for 500-800 words.
- Tone: honest, constructive, direct.
- Don't moralize. Explain mechanically what went wrong.
- If a `## Recent match history` or `## Cross-match patterns` section is supplied, call out recurring patterns by name (same hero killing them repeatedly, same itemization mistake, same hero played).
"""


def _phase_of(t_str: str) -> str:
    t_min = int(t_str.split(":")[0])
    return "early" if t_min < 8 else ("mid" if t_min < 15 else "late")


_LANE_ROLE = {1: "safe", 2: "mid", 3: "off", 4: "jungle"}


def _mmss(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _farm_snapshots(p: dict, minutes=(5, 10, 15)) -> list[str]:
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
        "hero_id": p.get("hero_id"),
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


def _baseline_beats(you: dict, match_rank: int | None, baselines: dict) -> list[str]:
    """For each KEY_ITEM the user bought, compare timing to the bracket median."""
    if not baselines.get("samples"):
        return []
    hero_id = you.get("hero_id")
    if not hero_id:
        return []
    out: list[str] = []
    for item in (you.get("purchase_log") or []):
        key = item.get("key", "")
        if key not in analyze_mod.KEY_ITEMS:
            continue
        baseline = baselines_mod.lookup(baselines, match_rank, hero_id, key)
        if not baseline:
            continue
        delta_str = baselines_mod.format_delta(int(item["time"]), baseline)
        out.append(f"- {analyze_mod.KEY_ITEMS[key]}: {delta_str}")
    return out


def _build_beats(report: dict, match: dict, account_id: int, baselines: dict) -> dict:
    you = report["you"]
    decisions = report["decisions"]
    all_decisions = decisions["biggest_leaks"] + decisions["kept_doing_this"]

    by_phase = {"early": [], "mid": [], "late": []}
    for d in all_decisions:
        by_phase[_phase_of(d["t"])].append(d)

    patterns = []
    deaths = [d for d in all_decisions if d["type"] == "death"]
    # Death "detail" may now be "Died to X" or "Died Nx in team fight (to X, Y)" from clustering.
    killer_counts: Counter = Counter()
    for d in deaths:
        det = d["detail"]
        if det.startswith("Died to "):
            killer_counts[det.removeprefix("Died to ")] += 1
        elif " (to " in det:
            enemy_part = det.split(" (to ", 1)[1].rstrip(")")
            for name in (n.strip() for n in enemy_part.split(",")):
                if name and "+" not in name:
                    killer_counts[name] += 1
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
    you_player = fetcher.player_for(match, account_id)
    your_team_radiant = (you_player.get("player_slot") or 0) < 128

    teammates: list[dict] = []
    enemies: list[dict] = []
    for p in match.get("players") or []:
        if p.get("account_id") == account_id:
            continue
        prof = _player_profile(p, heroes)
        is_radiant = (p.get("player_slot") or 0) < 128
        (teammates if is_radiant == your_team_radiant else enemies).append(prof)

    match_rank = report.get("match", {}).get("avg_rank_tier") or fetcher.avg_rank_tier(match)
    return {
        "summary": {
            **you,
            "duration_min": report["duration_min"],
            "match_id": report["match_id"],
            "patch": match.get("patch"),
            "avg_rank_tier": match_rank,
            "calibrated": report.get("match", {}).get("calibrated"),
        },
        "by_phase": by_phase,
        "patterns": patterns,
        "teammates": teammates,
        "enemies": enemies,
        "smoke_events": _smoke_events(match, heroes, your_team_radiant),
        "kill_timeline": _kill_timeline(match),
        "win_prob_curve": curve,
        "comeback_note": _comeback_note(curve, you["result"] == "win"),
        "baseline_beats": _baseline_beats(you_player, match_rank, baselines),
    }


def _format_decision_lines(items: list) -> list[str]:
    if not items:
        return ["  (no notable events in this phase)"]
    lines = []
    for d in items:
        sign = "+" if d["impact"] >= 0 else ""
        cluster = f" [team fight ×{d['cluster_size']}]" if d.get("cluster_size") else ""
        replay = f"  ({d['replay_url']})" if d.get("replay_url") else ""
        lines.append(
            f"  - {d['t']} | {d['type']:<7} | Δwp {sign}{d['impact']:.1%} | "
            f"{d['detail']}{cluster}{replay}"
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
        f"- You: **{s['hero']}** on **{s['team']}** (lane: {s.get('lane_role', '?')}), KDA **{s['kda']}**",
        f"- Result: **{s['result'].upper()}**",
        f"- Duration: {s['duration_min']} min  |  Avg rank tier: {s.get('avg_rank_tier')}  |  Patch: {s.get('patch')}"
        + ("  |  win-prob bracket-calibrated" if s.get("calibrated") else ""),
    ]

    lines.append("\n## Hero profiles (KDA / GPM / XPM / net worth / lane / key items with timings)")
    lines.append("\n### Your team (excluding you)")
    for prof in beats["teammates"]:
        lines.append(_format_profile_line(prof))
    lines.append("\n### Enemy team")
    for prof in beats["enemies"]:
        lines.append(_format_profile_line(prof))

    if beats.get("baseline_beats"):
        lines.append("\n## Item timings vs bracket baseline")
        lines.extend(beats["baseline_beats"])
        lines.append(
            "  (Negative delta = bought EARLIER than median; positive = LATER. "
            "Use these to identify items that came online too late vs your bracket's typical pace.)"
        )

    curve = beats.get("win_prob_curve") or []
    if curve:
        curve_str = ", ".join(f"{i:02d}:{p:.0%}" for i, p in enumerate(curve))
        lines.append(f"\n## Win-prob curve (your perspective, per minute)\n{curve_str}")

    lines.append(f"\n## Kill timeline (R = Radiant kills, D = Dire kills per minute)\n{beats['kill_timeline']}")

    if beats["smoke_events"]:
        lines.append("\n## Smoke events (gank initiation attempts)")
        for ev in beats["smoke_events"]:
            lines.append(f"- {ev}")
    else:
        lines.append("\n## Smoke events\n- (no smokes bought by either team)")

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
        "reference enemy heroes by name, their actual items + timings, real farm snapshots, "
        "real win-prob numbers, and bracket-baseline timing deltas where listed."
    )
    return "\n".join(lines)


def _load_memory(account_id: int) -> dict:
    path = config.memory_path(account_id)
    if not path.exists():
        return {"account_id": account_id, "history": []}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"account_id": account_id, "history": []}


def _save_memory(account_id: int, mem: dict) -> None:
    config.memory_path(account_id).write_text(json.dumps(mem, indent=2))


def _extract_themes(report: dict) -> list[str]:
    themes: list[str] = []
    leaks = report["decisions"]["biggest_leaks"]
    kept = report["decisions"]["kept_doing_this"]

    leak_types = Counter(d["type"] for d in leaks)
    for kind, _ in leak_types.most_common(2):
        themes.append(f"leak:{kind}")

    death_killers: Counter = Counter()
    for d in leaks:
        if d["type"] != "death":
            continue
        det = d["detail"]
        if det.startswith("Died to "):
            death_killers[det.removeprefix("Died to ")] += 1
        elif " (to " in det:
            enemies_part = det.split(" (to ", 1)[1].rstrip(")")
            for name in (n.strip() for n in enemies_part.split(",")):
                if name and "+" not in name:
                    death_killers[name] += 1
    for name, count in death_killers.items():
        if count >= 2:
            themes.append(f"repeat-deaths:{name}")

    if kept:
        themes.append(f"kept:{kept[0]['type']}")

    return themes


def _enemy_heroes(match: dict, account_id: int) -> list[int]:
    """Hero IDs on the enemy team for this match."""
    you = fetcher.player_for(match, account_id)
    if not you:
        return []
    your_radiant = (you.get("player_slot") or 0) < 128
    out = []
    for p in match.get("players") or []:
        is_radiant = (p.get("player_slot") or 0) < 128
        if is_radiant != your_radiant and p.get("hero_id"):
            out.append(p["hero_id"])
    return out


def _format_memory_for_prompt(
    mem: dict, current_hero: str | None, current_patch: int | None,
    current_enemies_hero_names: list[str],
) -> str:
    """Three blocks: recent history (last N) + same-hero history + recurring-matchup flags.

    Entries from older patches are auto-decayed (filtered out) from recent + same-hero
    blocks; matchup flags still aggregate across patches because the matchup itself
    rarely changes balance-wise.
    """
    history = mem.get("history") or []
    if not history:
        return ""

    def patch_ok(h: dict) -> bool:
        if not current_patch or not h.get("patch"):
            return True  # be permissive when patch info is missing
        return int(h["patch"]) >= int(current_patch)

    recent = [h for h in history if patch_ok(h)][-MEMORY_IN_PROMPT:]
    same_hero = [h for h in history if patch_ok(h) and h.get("hero") == current_hero][-MEMORY_SAME_HERO_LIMIT:]

    death_counts: Counter = Counter()
    for h in history:
        for theme in h.get("themes") or []:
            if theme.startswith("repeat-deaths:"):
                death_counts[theme.split(":", 1)[1]] += 1
    matchup_flags = [
        f"You've been repeatedly killed by {name} across {count} prior reviews "
        f"({'in this game too' if name in current_enemies_hero_names else 'not in this match'})."
        for name, count in death_counts.most_common(5)
        if count >= MEMORY_MATCHUP_THRESHOLD
    ]

    blocks: list[str] = []
    if recent:
        blocks.append("\n## Recent match history (your last reviewed games, oldest → newest)")
        for h in recent:
            blocks.append(_history_line(h))
    if same_hero and (current_hero != "?" and current_hero is not None):
        blocks.append(f"\n## Same-hero history ({current_hero}, last {len(same_hero)} reviews)")
        for h in same_hero:
            blocks.append(_history_line(h))
    if matchup_flags:
        blocks.append("\n## Cross-match patterns (recurring matchups)")
        for line in matchup_flags:
            blocks.append(f"- {line}")
    return "\n".join(blocks)


def _history_line(h: dict) -> str:
    themes = ", ".join(h.get("themes") or []) or "(none)"
    patch = h.get("patch")
    patch_str = f" [patch {patch}]" if patch else ""
    return (
        f"- {h.get('date', '?')}{patch_str} | {h.get('hero', '?')} | "
        f"{h.get('result', '?')} | KDA {h.get('kda', '?')} | themes: {themes}"
    )


def _append_to_memory(mem: dict, report: dict, match: dict) -> None:
    mem.setdefault("history", []).append({
        "match_id": report["match_id"],
        "date": time.strftime("%Y-%m-%d"),
        "hero": report["you"]["hero"],
        "result": report["you"]["result"],
        "kda": report["you"]["kda"],
        "patch": match.get("patch"),
        "themes": _extract_themes(report),
    })
    mem["history"] = mem["history"][-MEMORY_LIMIT:]


def _resolve_api_key(account_id: int) -> tuple[str, bool]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT anthropic_key_encrypted FROM users WHERE account_id = %s",
            (account_id,),
        ).fetchone()
    if row and row[0]:
        return crypto.decrypt_key(row[0]), True
    server_key = (config.ANTHROPIC_API_KEY or "").strip()
    if not server_key:
        raise SystemExit(
            "No Anthropic API key available. Either set ANTHROPIC_API_KEY in .env "
            "(server default) or save a personal key in Settings."
        )
    return server_key, False


def coach(
    match_id: int,
    account_id: int | None = None,
    model: str = "sonnet",
    top_k: int = 6,
    min_impact: float = 0.005,
    on_chunk: Callable[[str], None] | None = None,
) -> dict:
    aid = config.resolve_account_id(account_id)
    api_key, use_byo = _resolve_api_key(aid)
    cost.check_budget(aid, use_byo)

    report = analyze_mod.analyze(match_id, account_id=aid, top_k=top_k, min_impact=min_impact)
    match = fetcher.fetch_match(match_id)
    bsls = baselines_mod.load()
    beats = _build_beats(report, match, aid, bsls)

    memory = _load_memory(aid)
    enemy_names = [
        analyze_mod.heroes_by_id().get(h, {}).get("localized_name", "?")
        for h in _enemy_heroes(match, aid)
    ]
    memory_block = _format_memory_for_prompt(
        memory,
        current_hero=report["you"]["hero"],
        current_patch=match.get("patch"),
        current_enemies_hero_names=enemy_names,
    )
    user_prompt = _build_user_prompt(beats) + memory_block

    model_id = MODEL_ALIASES.get(model, model)
    client = anthropic.Anthropic(api_key=api_key)

    markdown = ""
    usage: dict
    try:
        if on_chunk is None:
            resp = client.messages.create(
                model=model_id,
                max_tokens=3500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                cache_control={"type": "ephemeral"},
            )
            markdown = next((b.text for b in resp.content if b.type == "text"), "")
            usage = {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
            }
        else:
            with client.messages.stream(
                model=model_id,
                max_tokens=3500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                for text in stream.text_stream:
                    markdown += text
                    on_chunk(text)
                final = stream.get_final_message()
            usage = {
                "input_tokens": final.usage.input_tokens,
                "output_tokens": final.usage.output_tokens,
                "cache_read_input_tokens": getattr(final.usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(final.usage, "cache_creation_input_tokens", 0),
            }
    except anthropic.AuthenticationError as e:
        raise SystemExit(f"Anthropic authentication failed — check the API key. ({e.message})")
    except anthropic.RateLimitError as e:
        raise SystemExit(f"Rate limited by Anthropic API: {e.message}")
    except anthropic.APIStatusError as e:
        raise SystemExit(f"Anthropic API error ({e.status_code}): {e.message}")

    out_path = config.reviews_dir_for(aid) / f"{match_id}.md"
    out_path.write_text(markdown)

    _append_to_memory(memory, report, match)
    _save_memory(aid, memory)

    cents = cost.estimate_cents(model_id, usage)
    cost.charge(aid, cents, use_byo_key=use_byo)

    return {
        "match_id": match_id,
        "account_id": aid,
        "model": model_id,
        "review_path": str(out_path),
        "memory_entries": len(memory["history"]),
        "byo_key": use_byo,
        "cost_cents": cents,
        "streamed": on_chunk is not None,
        "usage": usage,
    }
