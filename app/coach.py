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

SYSTEM_PROMPT = """You are a Dota 2 Turbo coach reviewing a match for a single player.

You receive structured findings from a win-probability model and raw match context. Speak as a knowledgeable friend talking through the replay — direct, constructive, never falsely encouraging.

Your response must be markdown with:
- A 1-2 sentence opening (hero, result, framing).
- 2-4 short paragraphs of narrative review organized by game phase (early ≤8 min, mid 8-15 min, late >15 min). Skip a phase if nothing notable happened there.
- A **Three takeaways** section listing 3 specific, actionable items the player should focus on next time.

Rules:
- Only reference events listed in the findings. Do not invent events or stats.
- Cite timestamps in MM:SS format when relevant.
- Cite win-prob deltas only when meaningful (|Δ| ≥ 5%).
- Keep total response under ~500 words. Coaches don't ramble.
- Tone: honest, constructive, direct. Don't sugarcoat losses but don't pile on either.
- If a "Recent match history" section is supplied, look for recurring patterns (same hero killing them repeatedly across matches, same itemization mistake, same hero played) and call them out by name — that's where a coach earns their keep.
"""


def _phase_of(t_str: str) -> str:
    t_min = int(t_str.split(":")[0])
    return "early" if t_min < 8 else ("mid" if t_min < 15 else "late")


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
            f"Your win-probability peaked at {curve[peak_i]:.0%} around minute {peak_i:02d} "
            f"and bottomed at {curve[trough_i]:.0%} around minute {trough_i:02d}."
        )

    heroes = analyze_mod.heroes_by_id()
    you_player = fetcher.your_player(match)
    your_team_radiant = (you_player.get("player_slot") or 0) < 128
    teammates: list[str] = []
    enemies: list[str] = []
    for p in match.get("players") or []:
        if p.get("account_id") == config.require_account_id():
            continue
        hero_name = heroes.get(p.get("hero_id"), {}).get("localized_name", "?")
        is_radiant = (p.get("player_slot") or 0) < 128
        (teammates if is_radiant == your_team_radiant else enemies).append(hero_name)

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


def _build_user_prompt(beats: dict) -> str:
    s = beats["summary"]
    lines = [
        f"## Match {s['match_id']}",
        f"- You: **{s['hero']}** on **{s['team']}**, KDA **{s['kda']}**",
        f"- Result: **{s['result'].upper()}**",
        f"- Duration: {s['duration_min']} min  |  Avg rank tier: {s.get('avg_rank_tier')}  |  Patch: {s.get('patch')}",
        f"- Teammates: {', '.join(beats['teammates']) or '?'}",
        f"- Enemies:   {', '.join(beats['enemies']) or '?'}",
        "",
        "## Scored decisions by phase",
    ]
    for phase in ("early", "mid", "late"):
        lines.append(f"\n### {phase.capitalize()} game (impact already signed for your team)")
        lines.extend(_format_decision_lines(beats["by_phase"][phase]))

    if beats["patterns"]:
        lines.append("\n## Notable patterns")
        for p in beats["patterns"]:
            lines.append(f"- {p}")

    lines.append("\nWrite the coach review now.")
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
            max_tokens=2000,
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
