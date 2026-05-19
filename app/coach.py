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
    """Append the just-reviewed match to memory. Re-reviewing the same match replaces
    its prior entry instead of duplicating."""
    history = mem.setdefault("history", [])
    mid = report["match_id"]
    history[:] = [h for h in history if h.get("match_id") != mid]
    history.append({
        "match_id": mid,
        "date": time.strftime("%Y-%m-%d"),
        "hero": report["you"]["hero"],
        "result": report["you"]["result"],
        "kda": report["you"]["kda"],
        "patch": match.get("patch"),
        "themes": _extract_themes(report),
    })
    mem["history"] = history[-MEMORY_LIMIT:]


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


PER_LEAK_SYSTEM_PROMPT = """You are a Dota 2 Turbo coach analyzing specific bad decisions ("leaks") from one match. For each numbered leak you receive, give ONE concrete, tactical recommendation explaining what the player could have done differently.

CRITICAL: every leak context lists events from BEFORE the leak only — kills "LEADING UP TO" and smokes "PRECEDING" the engagement. **Never reference events whose timestamp is later than the leak's timestamp.** All causal reasoning must be backward-looking. If a context only lists pre-leak events, do not invent post-leak ones.

Rules:
- Be SPECIFIC: reference enemy heroes by name, items they had at that moment, win-prob shifts.
- AVOID generic advice ("stay safe", "play smarter", "be more careful"). Always give actionable specifics about THIS situation.
- Aim for 1-2 sentences, under 50 words each.
- Focus on positioning, item timing, vision, cooldown windows, baiting, smoke timing — all causally upstream of the leak.
- Output ONLY a JSON object mapping the leak number (string) to recommendation text. No prose, no markdown fences.

Example output:
{"1": "Spectre had Radiance by 9:00 and was farming alone in your jungle. With Pudge's Dismember on cooldown after 8:30, that was the obvious smoke-gank window for your support to call.", "2": "QoP's Eul's was up from 13:00; her burst combo needs Blink-Scream-Bolt. You walked unblinked at 14:00 with no escape — should have stayed under T2 until your own Blink at 14:16 before leaving creep cover."}
"""


def _level_at_time(player: dict, t_sec: int) -> int:
    """Approximate level from xp_t per-minute array."""
    xp_t = player.get("xp_t") or []
    if not xp_t:
        return 1
    minute = max(0, min(t_sec // 60, len(xp_t) - 1))
    xp = xp_t[minute]
    thresholds = (0, 230, 600, 1080, 1660, 2260, 2980, 3730, 4510, 5320,
                  6160, 7030, 7930, 8860, 9820, 10810, 11830, 12880, 13960,
                  15080, 16220, 17390, 18580, 19790, 21020, 22270, 23540, 24830)
    for lvl in range(len(thresholds) - 1, -1, -1):
        if xp >= thresholds[lvl]:
            return lvl + 1
    return 1


def _items_at_time(player: dict, t_sec: int) -> list[str]:
    """KEY_ITEMS the player had by time t_sec."""
    items = []
    for p in player.get("purchase_log") or []:
        if p.get("time", 0) <= t_sec and p.get("key") in analyze_mod.KEY_ITEMS:
            items.append(f"{analyze_mod.KEY_ITEMS[p['key']]}@{_mmss(p['time'])}")
    return items


def _gold_at_time(player: dict, t_sec: int) -> int:
    gold_t = player.get("gold_t") or []
    if not gold_t:
        return 0
    minute = max(0, min(t_sec // 60, len(gold_t) - 1))
    return gold_t[minute]


def _build_leak_context(
    leak_idx: int, leak: dict, match: dict, heroes: dict,
    account_id: int, your_radiant: bool, curve: list[float],
) -> str:
    """Compact context block for one leak — what the LLM needs to reason."""
    t_str = leak["t"]
    parts = t_str.split(":")
    t_sec = int(parts[0]) * 60 + int(parts[1])
    minute = t_sec // 60

    def _wp_at(m):
        if not curve:
            return 0.0
        return curve[max(0, min(len(curve) - 1, m))]
    wp_before = _wp_at(minute - 2)
    wp_at = _wp_at(minute)
    wp_after = _wp_at(minute + 2)

    you = fetcher.player_for(match, account_id)
    you_hero = heroes.get(you.get("hero_id"), {}).get("localized_name", "?")
    you_lvl = _level_at_time(you, t_sec)
    you_items = _items_at_time(you, t_sec)
    you_gold = _gold_at_time(you, t_sec)

    enemy_lines = []
    for p in match.get("players") or []:
        is_radiant = (p.get("player_slot") or 0) < 128
        if is_radiant == your_radiant:
            continue
        h = heroes.get(p.get("hero_id"), {}).get("localized_name", "?")
        lvl = _level_at_time(p, t_sec)
        items = _items_at_time(p, t_sec)
        gold = _gold_at_time(p, t_sec)
        kda = f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)}"
        items_str = ", ".join(items) if items else "(none)"
        enemy_lines.append(f"    {h:<18} L{lvl:<2} {gold:>5}g KDA {kda:<8} items: {items_str}")

    # Causal time window: only events that could have CAUSED this leak — i.e., pre-leak only.
    # We previously used [t-90, t+90] which let the LLM hallucinate "you were caught because
    # they smoked at t+47s" — impossible (the smoke happened AFTER the death).
    PRE_WINDOW = 120  # seconds of preceding history to surface

    # Kills BEFORE or at the leak moment — these set up / accompany the death.
    kills_before = []
    for p in match.get("players") or []:
        for k in p.get("kills_log") or []:
            kt = k.get("time", 0)
            if t_sec - PRE_WINDOW <= kt <= t_sec:
                killer = heroes.get(p.get("hero_id"), {}).get("localized_name", "?")
                victim_npc = k.get("key", "")
                victim = next(
                    (h["localized_name"] for h in heroes.values() if h["name"] == victim_npc),
                    victim_npc,
                )
                kills_before.append(f"    {_mmss(kt)}: {killer} → {victim}")

    # Smokes PRECEDING the leak — smokes initiate, they can't react-cause a death after-the-fact.
    smokes_before = []
    for p in match.get("players") or []:
        is_radiant = (p.get("player_slot") or 0) < 128
        side = "your" if is_radiant == your_radiant else "enemy"
        for item in p.get("purchase_log") or []:
            if item.get("key") == "smoke_of_deceit":
                st_sec = item["time"]
                if t_sec - PRE_WINDOW <= st_sec < t_sec:
                    h = heroes.get(p.get("hero_id"), {}).get("localized_name", "?")
                    smokes_before.append(f"    {_mmss(st_sec)}: smoke by {h} ({side} team)")

    blocks = [
        f"LEAK {leak_idx}:",
        f"  When: {t_str}  |  Type: {leak['type']}  |  Detail: {leak['detail']}  |  Δwp: {leak['impact']*100:+.1f}%",
        f"  Win-prob trajectory (your perspective): {wp_before:.0%} (min {minute-2}) → {wp_at:.0%} (min {minute}) → {wp_after:.0%} (min {minute+2})",
        f"  You: {you_hero}, L{you_lvl}, {you_gold}g, items: {', '.join(you_items) or '(none)'}",
        "  Enemy team at this moment:",
        *enemy_lines,
        f"  Kills in the {PRE_WINDOW}s LEADING UP TO this leak (causal context only):",
        *(kills_before or ["    (none)"]),
        "  Smokes PRECEDING this leak (smokes that may have set up the engagement):",
        *(smokes_before or ["    (none)"]),
    ]
    return "\n".join(blocks)


def _format_per_leak_prompt(report: dict, leak_contexts: list[str]) -> str:
    you = report["you"]
    match_meta = report.get("match", {})
    rt = match_meta.get("avg_rank_tier")
    bracket = {1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
               5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal"}.get(rt // 10 if rt else 0, "?")
    parts = [
        f"# Match {report['match_id']}",
        f"You: {you['hero']} (lane: {you.get('lane_role', '?')}) on {you['team']}, KDA {you['kda']}",
        f"Result: {you['result'].upper()}  |  Duration: {report['duration_min']} min  |  Bracket: {bracket} (rank tier {rt})",
        "",
        "Below are the biggest leaks in this game. For each leak, write ONE concrete tactical recommendation (1-2 sentences, under 50 words) explaining what the player could have done differently. Reference specific items, enemy heroes, win-prob, vision, cooldown windows. NO generic advice.",
        "",
        "Reply with JSON only (no markdown fences, no prose around it):",
        '{"1": "...", "2": "...", ...}',
        "",
    ]
    parts.extend(leak_contexts)
    return "\n".join(parts)


def _parse_per_leak_response(text: str) -> dict:
    """Parse Claude's JSON response into a {index: text} dict. Tolerates markdown fences."""
    import re
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        data = json.loads(text)
        return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError:
        # Fallback: extract numbered patterns like '"1": "text"' or '1. text' or '1) text'
        out = {}
        for m in re.finditer(
            r'(?:^|[\n,])\s*"?(\d+)"?\s*[:.)]\s*"?([^\n]+?)"?\s*(?=[,\n]|$)',
            text,
        ):
            out[m.group(1)] = m.group(2).rstrip(',').rstrip('"').strip()
        return out


def recommend_per_leak(
    match_id: int,
    account_id: int | None = None,
    model: str = "haiku",
    top_k: int = 6,
    min_impact: float = 0.005,
) -> dict:
    """Generate per-leak tactical recommendations via Claude (Haiku by default).

    Caches to data/recommendations/<aid>/<mid>.json keyed by (match_id, account_id).
    Cost: ~$0.003-0.006 per call on Haiku 4.5.
    """
    aid = config.resolve_account_id(account_id)

    cache_path = config.recommendations_dir_for(aid) / f"{match_id}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("recommendations"):
                cached["from_cache"] = True
                cached["cost_cents"] = 0
                return cached
        except json.JSONDecodeError:
            pass

    report = analyze_mod.analyze(match_id, account_id=aid, top_k=top_k, min_impact=min_impact)
    leaks = report["decisions"]["biggest_leaks"]
    if not leaks:
        result = {
            "match_id": match_id, "account_id": aid, "model": model,
            "recommendations": {}, "leaks_count": 0, "from_cache": False,
            "cost_cents": 0, "note": "no leaks to recommend on",
        }
        cache_path.write_text(json.dumps(result, indent=2))
        return result

    api_key, use_byo = _resolve_api_key(aid)
    cost.check_budget(aid, use_byo)

    match = fetcher.fetch_match(match_id)
    heroes = analyze_mod.heroes_by_id()
    you = fetcher.player_for(match, aid)
    your_radiant = (you.get("player_slot") or 0) < 128
    curve = report.get("win_prob_curve") or []

    leak_contexts = [
        _build_leak_context(i + 1, leak, match, heroes, aid, your_radiant, curve)
        for i, leak in enumerate(leaks)
    ]
    user_prompt = _format_per_leak_prompt(report, leak_contexts)

    model_id = MODEL_ALIASES.get(model, model)
    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=model_id,
            max_tokens=1500,
            system=PER_LEAK_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise SystemExit(f"Anthropic authentication failed: {e.message}")
    except anthropic.RateLimitError as e:
        raise SystemExit(f"Rate limited by Anthropic: {e.message}")
    except anthropic.APIStatusError as e:
        raise SystemExit(f"Anthropic API error ({e.status_code}): {e.message}")

    raw_text = next((b.text for b in resp.content if b.type == "text"), "")
    recommendations = _parse_per_leak_response(raw_text)

    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
    }
    cents = cost.estimate_cents(model_id, usage)
    cost.charge(aid, cents, use_byo_key=use_byo)

    result = {
        "match_id": match_id,
        "account_id": aid,
        "model": model_id,
        "recommendations": recommendations,
        "leaks_count": len(leaks),
        "from_cache": False,
        "byo_key": use_byo,
        "cost_cents": cents,
        "usage": usage,
    }
    cache_path.write_text(json.dumps(result, indent=2))
    return result


BLAME_SYSTEM_PROMPT = """You are the Stanley Parable narrator: dry, mock-formal, quietly devastating. Write a **2-3 sentence zinger** blaming ONE Dota 2 player for the entire loss.

Hard constraints:
- **30–50 words. 2–3 sentences. That's the ceiling.**
- The user prompt lists role-normalized "blame factors" — pick the stat with the **biggest positive deviation** (deaths/gpm/hero_dmg/tower_dmg) and build the joke around it. Raw counts are misleading: a support with 12 deaths is fine; a carry with 8 may be a disaster.
- Reference the player's **role** (Pos 1 carry, support, mid, etc.) when it sharpens the bit.
- Hero name only, never real names.
- Stanley Parable voice: mock-formal, quiet contempt, one "And yet" / "Curiously" / "For reasons known only to themselves" structural beat.
- No slurs, no real-world insults. The toxicity is precision.
- No preamble, no headers — output only the zinger.

**Phrases to AVOID** (Haiku tends to overuse these — they've become a tic):
- "The narrator has reviewed…"
- "The narrator observes…"
- "The narrator notes…"
- "The narrator has questions."
- "The narrator regrets…"
Use ANY of these at most ONCE per zinger, and only if it's funnier than a data-anchored alternative. **At most one "the narrator…" reference per zinger. Zero is better.**

**Prefer data-anchored phrasing**: open with the stat, not with the narrator. Examples:
- "As per the death timeline, …"
- "By minute 14, the Sniper had …"
- "Across thirty-two minutes of available farm, …"
- "Of the four kills the Mid registered, three came in the same teamfight at 11:30 …"
- "In a hero who scales linearly with right-clicks, that GPM is a choice."

Good examples (note role + data-anchored opener):

> *The Sniper — a Position 1 carry, allegedly — finished with 14k hero damage. The enemy Pudge, a support, did more. Both heroes have ranged abilities. One was used.*

> *And yet the Storm Spirit, holding the team's mid lane and 41% of its gold, ended the match at 350 GPM. In a 32-minute Turbo. With Boots of Travel.*

> *For reasons known only to themselves, the Crystal Maiden — a hard support, granted — accumulated 12 deaths. The team's Position 1 carry had 4. **The wrong hero was carrying.***

Bad (over-uses narrator self-reference):
> "The narrator has reviewed the death timeline. The narrator observes no learning curve. The narrator has questions." — That's three uses, zero data. Anchor in the data instead.
"""


def _player_summary_for_blame(p: dict, heroes: dict) -> dict:
    hero = heroes.get(p.get("hero_id"), {}).get("localized_name", "?")
    items = []
    for item in (p.get("purchase_log") or []):
        if item.get("key") in analyze_mod.KEY_ITEMS:
            items.append(f"{analyze_mod.KEY_ITEMS[item['key']]}@{_mmss(item['time'])}")
    return {
        "hero": hero,
        "hero_id": p.get("hero_id"),
        "account_id": p.get("account_id"),
        "player_slot": p.get("player_slot"),
        "lane_role": _LANE_ROLE.get(p.get("lane_role")) or "?",
        "kills": p.get("kills", 0),
        "deaths": p.get("deaths", 0),
        "assists": p.get("assists", 0),
        "gpm": p.get("gold_per_min", 0),
        "xpm": p.get("xp_per_min", 0),
        "net_worth": p.get("net_worth", 0),
        "last_hits": p.get("last_hits", 0),
        "denies": p.get("denies", 0),
        "hero_damage": p.get("hero_damage", 0),
        "tower_damage": p.get("tower_damage", 0),
        "items": items,
    }


# Role baselines for Turbo at average rank. These are rough by design — the
# composite score uses z-score-style deviations, not exact matching.
_ROLE_BASELINES = {
    "carry": {"deaths": 4,  "gpm": 600, "hero_dmg": 25000, "tower_dmg": 5000},
    "mid":   {"deaths": 4,  "gpm": 580, "hero_dmg": 26000, "tower_dmg": 4000},
    "off":   {"deaths": 6,  "gpm": 460, "hero_dmg": 22000, "tower_dmg": 3000},
    "sup4":  {"deaths": 8,  "gpm": 380, "hero_dmg": 16000, "tower_dmg": 1500},
    "sup5":  {"deaths": 10, "gpm": 320, "hero_dmg": 14000, "tower_dmg": 1200},
}

# Component weights for the composite blame score. Tuned for funny + fair:
#   deaths matter a lot (most visceral), farming matters (you didn't even farm),
#   damage matters (you weren't in fights), tower damage matters less but counts.
_BLAME_WEIGHTS = {"deaths": 2.0, "gpm": 1.5, "hero_dmg": 1.0, "tower_dmg": 0.8}


def _infer_role(player: dict, team: list[dict]) -> str:
    """Infer role from GPM rank within the team.

    Highest GPM → carry, lowest → hard support. Approximate but reliable enough
    that supports stop getting hammered just for dying a lot.
    """
    sorted_team = sorted(team, key=lambda p: -(p.get("gold_per_min") or 0))
    try:
        rank = sorted_team.index(player)
    except ValueError:
        rank = 4
    return ("carry", "mid", "off", "sup4", "sup5")[min(rank, 4)]


def _blame_score(player: dict, role: str) -> tuple[float, dict]:
    """Composite blame score (higher = more blameable) + per-factor breakdown.

    Each factor is a z-score-like deviation from the role baseline. Positive = bad
    (more deaths than expected, less farm than expected, etc.). Weighted sum.
    """
    base = _ROLE_BASELINES[role]
    deaths = player.get("deaths", 0)
    gpm = player.get("gold_per_min", 0)
    hero_dmg = player.get("hero_damage", 0)
    tower_dmg = player.get("tower_damage", 0)

    death_z = (deaths - base["deaths"]) / base["deaths"]
    gpm_z = (base["gpm"] - gpm) / base["gpm"]
    dmg_z = (base["hero_dmg"] - hero_dmg) / base["hero_dmg"]
    tower_z = (base["tower_dmg"] - tower_dmg) / base["tower_dmg"]

    score = (
        _BLAME_WEIGHTS["deaths"]    * death_z +
        _BLAME_WEIGHTS["gpm"]       * gpm_z   +
        _BLAME_WEIGHTS["hero_dmg"]  * dmg_z   +
        _BLAME_WEIGHTS["tower_dmg"] * tower_z
    )
    factors = {
        "deaths":      {"value": deaths,    "baseline": base["deaths"],   "z": round(death_z, 2)},
        "gpm":         {"value": gpm,       "baseline": base["gpm"],      "z": round(gpm_z, 2)},
        "hero_damage": {"value": hero_dmg,  "baseline": base["hero_dmg"], "z": round(dmg_z, 2)},
        "tower_damage":{"value": tower_dmg, "baseline": base["tower_dmg"],"z": round(tower_z, 2)},
    }
    return score, factors


def _pick_blame_target(match: dict, losing_team_radiant: bool,
                       exclude_account: int | None = None) -> tuple[dict, str, dict]:
    """Pick the player on the losing team most deserving of blame.

    Role-aware composite score: a carry with 8 deaths is more blame-worthy than a
    support with 12 deaths because deaths are normalized by role expectations.
    """
    losing_team = [
        p for p in (match.get("players") or [])
        if ((p.get("player_slot") or 0) < 128) == losing_team_radiant
    ]
    pool = [p for p in losing_team if p.get("account_id") != exclude_account]
    if not pool:
        pool = losing_team

    candidates = []
    for p in pool:
        role = _infer_role(p, losing_team)
        score, factors = _blame_score(p, role)
        candidates.append((score, p, role, factors))

    _score, target, role, factors = max(candidates, key=lambda c: c[0])
    return target, role, factors


def _format_blame_prompt(match: dict, target: dict, heroes: dict,
                         losing_team_radiant: bool,
                         role: str | None = None, factors: dict | None = None) -> str:
    target_sum = _player_summary_for_blame(target, heroes)

    # Teammates of target (same losing side, excluding the target)
    teammates = []
    enemies = []
    for p in match.get("players") or []:
        if p is target:
            continue
        is_radiant = (p.get("player_slot") or 0) < 128
        if is_radiant == losing_team_radiant:
            teammates.append(_player_summary_for_blame(p, heroes))
        else:
            enemies.append(_player_summary_for_blame(p, heroes))

    # Who killed the target, when
    target_hero_npc = heroes.get(target.get("hero_id"), {}).get("name", "")
    deaths_log = []
    for p in match.get("players") or []:
        for k in p.get("kills_log") or []:
            if k.get("key") == target_hero_npc:
                killer = heroes.get(p.get("hero_id"), {}).get("localized_name", "?")
                deaths_log.append(f"  - {_mmss(k.get('time', 0))}: killed by {killer}")

    team_avg_gpm = sum(t["gpm"] for t in teammates) / max(1, len(teammates))
    team_avg_kda = sum(
        ((t["kills"] + t["assists"]) / max(1, t["deaths"])) for t in teammates
    ) / max(1, len(teammates))
    target_kda = (target_sum["kills"] + target_sum["assists"]) / max(1, target_sum["deaths"])

    role_display = {
        "carry": "Position 1 / Carry",
        "mid":   "Position 2 / Mid",
        "off":   "Position 3 / Offlane",
        "sup4":  "Position 4 / Soft Support",
        "sup5":  "Position 5 / Hard Support",
    }.get(role or "", "?")

    lines = [
        f"# Match {match.get('match_id')}",
        f"Game duration: {(match.get('duration') or 0) // 60} min",
        "Result for the team being analyzed: **LOSS**",
        "",
        "## The accused",
        f"  Hero:           **{target_sum['hero']}**",
        f"  Inferred role:  **{role_display}** (by GPM rank within their team)",
        f"  KDA:            {target_sum['kills']}/{target_sum['deaths']}/{target_sum['assists']}  (ratio {target_kda:.2f})",
        f"  GPM / XPM:      {target_sum['gpm']} / {target_sum['xpm']}",
        f"  Net worth:      {target_sum['net_worth']}",
        f"  Last hits:      {target_sum['last_hits']}  |  Denies: {target_sum['denies']}",
        f"  Hero damage:    {target_sum['hero_damage']}",
        f"  Tower damage:   {target_sum['tower_damage']}",
        f"  Items:          {', '.join(target_sum['items']) or '(no key items)'}",
    ]
    if factors:
        lines.append("")
        lines.append("## Why this player was picked (role-normalized blame factors)")
        for name, f in factors.items():
            sign = "+" if f["z"] >= 0 else ""
            lines.append(
                f"  {name:<12}: {f['value']:>6}  vs role baseline {f['baseline']:>6}  "
                f"({sign}{f['z']*100:.0f}% deviation, {'BAD' if f['z'] > 0 else 'fine'})"
            )
        lines.append("  (Pick THE most damning stat — usually the biggest positive deviation — for your zinger.)")

    lines.append("")
    lines.append("## Their teammates (for comparison — they all also lost, but with dignity)")
    for t in teammates:
        kda = f"{t['kills']}/{t['deaths']}/{t['assists']}"
        items = ", ".join(t["items"]) or "(no key items)"
        lines.append(
            f"  - {t['hero']:<18} lane {t['lane_role']:<6} KDA {kda:<8} "
            f"GPM {t['gpm']:<4} NW {t['net_worth']:<6} items: {items}"
        )
    lines.append("")
    lines.append("## Their team average (excluding the subject)")
    lines.append(f"  Avg GPM (teammates):     {team_avg_gpm:.0f}")
    lines.append(f"  Avg KDA ratio (teammates): {team_avg_kda:.2f}")
    lines.append(f"  Subject's deficit vs team-GPM: {team_avg_gpm - target_sum['gpm']:.0f}")
    lines.append("")
    lines.append("## Enemy team (the team that won, in case the subject blames them)")
    for e in enemies:
        kda = f"{e['kills']}/{e['deaths']}/{e['assists']}"
        items = ", ".join(e["items"]) or "(no key items)"
        lines.append(
            f"  - {e['hero']:<18} KDA {kda:<8} GPM {e['gpm']:<4} items: {items}"
        )
    lines.append("")
    lines.append("## Death timeline for the accused")
    lines.extend(deaths_log or ["  (they died zero times. Find another angle.)"])
    lines.append("")
    lines.append("Write the blame narrative now. Three to four paragraphs in the Stanley Parable narrator's voice. The accused is the ONLY reason the game was lost.")
    return "\n".join(lines)


def assign_blame(
    match_id: int,
    account_id: int | None = None,
    model: str = "haiku",
    target_slot: int | None = None,
    exclude_self: bool = True,
) -> dict:
    """Generate Stanley-Parable-style blame attribution for a loss.

    Picks the worst-statline player on the losing team (excluding the user by default;
    set `exclude_self=False` to allow self-blame) and asks Claude for a dryly devastating
    narrator paragraph attributing the entire loss to them.

    Caches to data/blame/<aid>/<mid>.json by (match_id, account_id, target_slot).
    Cost: ~$0.001-0.003 on Haiku 4.5.
    """
    aid = config.resolve_account_id(account_id)
    cache_key_slot = target_slot if target_slot is not None else "auto"
    cache_path = config.blame_dir_for(aid) / f"{match_id}__{cache_key_slot}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("blame_text"):
                cached["from_cache"] = True
                cached["cost_cents"] = 0
                return cached
        except json.JSONDecodeError:
            pass

    match = fetcher.fetch_match(match_id)
    you = fetcher.player_for(match, aid)
    if not you:
        raise SystemExit(f"account_id {aid} is not in match {match_id}")

    your_radiant = (you.get("player_slot") or 0) < 128
    you_won = bool(match["radiant_win"]) == your_radiant
    # The losing team — always our target pool. On a loss it's your side; on a
    # win it's the enemy side. The narrator's voice works either way: someone
    # lost the game, and we are about to point at them.
    losing_team_radiant = your_radiant if not you_won else not your_radiant

    heroes = analyze_mod.heroes_by_id()

    if target_slot is not None:
        target = next(
            (p for p in match.get("players") or [] if p.get("player_slot") == target_slot),
            None,
        )
        if not target:
            raise SystemExit(f"no player at slot {target_slot} in this match")
        target_radiant = (target.get("player_slot") or 0) < 128
        if target_radiant != losing_team_radiant:
            raise SystemExit("can only blame players on the losing team")
        # Compute role + factors even for manually-selected target, for prompt context
        losing_team_players = [
            p for p in (match.get("players") or [])
            if ((p.get("player_slot") or 0) < 128) == losing_team_radiant
        ]
        target_role = _infer_role(target, losing_team_players)
        _score, target_factors = _blame_score(target, target_role)
    else:
        # When you lost, exclude yourself by default (toxic-community traditional).
        # When you won, you're not on the losing pool anyway, so exclusion is a no-op.
        effective_exclude = aid if (exclude_self and not you_won) else None
        target, target_role, target_factors = _pick_blame_target(
            match,
            losing_team_radiant=losing_team_radiant,
            exclude_account=effective_exclude,
        )

    api_key, use_byo = _resolve_api_key(aid)
    cost.check_budget(aid, use_byo)

    user_prompt = _format_blame_prompt(
        match, target, heroes, losing_team_radiant=losing_team_radiant,
        role=target_role, factors=target_factors,
    )
    model_id = MODEL_ALIASES.get(model, model)
    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=model_id,
            max_tokens=200,   # ~50 words + slack — hard ceiling on rambling
            system=BLAME_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise SystemExit(f"Anthropic authentication failed: {e.message}")
    except anthropic.RateLimitError as e:
        raise SystemExit(f"Rate limited by Anthropic: {e.message}")
    except anthropic.APIStatusError as e:
        raise SystemExit(f"Anthropic API error ({e.status_code}): {e.message}")

    blame_text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
    }
    cents = cost.estimate_cents(model_id, usage)
    cost.charge(aid, cents, use_byo_key=use_byo)

    target_hero = heroes.get(target.get("hero_id"), {}).get("localized_name", "?")
    target_radiant = (target.get("player_slot") or 0) < 128
    target_on_your_team = target_radiant == your_radiant
    result = {
        "match_id": match_id,
        "account_id": aid,
        "model": model_id,
        "you_won": you_won,
        "target": {
            "hero": target_hero,
            "hero_id": target.get("hero_id"),
            "player_slot": target.get("player_slot"),
            "account_id": target.get("account_id"),
            "is_you": target.get("account_id") == aid,
            "team": "yours" if target_on_your_team else "enemy",
            "kda": f"{target.get('kills', 0)}/{target.get('deaths', 0)}/{target.get('assists', 0)}",
            "gpm": target.get("gold_per_min"),
            "lane_role": _LANE_ROLE.get(target.get("lane_role")) or "?",
            "inferred_role": target_role,
            "blame_factors": target_factors,
        },
        "blame_text": blame_text,
        "from_cache": False,
        "byo_key": use_byo,
        "cost_cents": cents,
        "usage": usage,
    }
    cache_path.write_text(json.dumps(result, indent=2))
    return result


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
