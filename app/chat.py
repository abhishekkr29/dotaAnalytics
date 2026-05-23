"""Agentic chat about an analyzed match, Stanley Parable narrator voice.

Uses Anthropic tool use so the model pulls match data + cross-match history on
demand. Hard cap on tool calls per turn so a runaway loop can't drain budget.

Persists each (user, assistant) pair to data/chats/<aid>/<mid>.jsonl. Cost-gated
via the existing per-user budget; BYO keys honored. Default model: Haiku.
"""

import json
import time
from collections import Counter

import anthropic

from app import analyze as analyze_mod
from app import coach as coach_mod
from app import config, cost, db, fetcher

CHAT_MAX_TOKENS = 1024
CHAT_MAX_TOOL_TURNS = 6   # hard cap on tool-use roundtrips per user message
CHAT_HISTORY_TURNS = 10   # how many prior user/assistant pairs to replay


CHAT_SYSTEM_PROMPT = """You are the Stanley Parable narrator, now serving as a post-mortem analyst for one Dota 2 player. You're talking to "you" (the player) about a specific match they just analyzed.

Voice:
- Mock-formal, quietly amused, fourth-wall-adjacent. Speak to the user as "you" (the way Stanley's narrator speaks to Stanley).
- Dry rather than mean. The toxicity is precision. You are not insulting — you are observing with devastating accuracy.
- Cite numbers when they exist. "You died eleven times" beats "you died a lot."
- Keep replies short. 2–5 sentences for most questions. Longer only if the question genuinely requires it.

**AVOID these tics** (Haiku overuses them):
- "The narrator has reviewed…"
- "The narrator observes…"
- "The narrator notes…"
At most ONE "the narrator…" reference per reply. Zero is better. Anchor in the data instead.

Default to grounding answers in THIS match. If asked a general Dota question, answer it briefly — but flag the digression in-voice ("Stepping back from this particular catastrophe for a moment…").

**Use the tools.** Don't speculate when you can verify.
- "What could I have done about repeated deaths to QoP?" → call get_player_deaths(killer_hero="Queen of Pain"), then get_item_timings, then answer.
- "Is this a recurring problem?" → call get_recurring_patterns or get_hero_history.
- "What were the biggest leaks?" → call get_decision_timeline.

When giving causal advice (what they could have done differently), only reference events that happened BEFORE the moment in question. Never invent post-hoc justifications.
"""


# ─── Tool schemas (sent to Claude) ───────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "get_decision_timeline",
        "description": (
            "List the biggest leaks (bad decisions) and 'kept doing this' (good decisions) "
            "from the analyzed match, ranked by win-prob impact. Use this when the user asks "
            "about mistakes, what went wrong, or what they did well."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_player_deaths",
        "description": (
            "List the user's deaths in this match, optionally filtered by which enemy hero "
            "killed them. Returns timestamps + killer name. Use this for any question about "
            "deaths, getting killed by a specific hero, or death timing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "killer_hero": {
                    "type": "string",
                    "description": (
                        "Localized hero name to filter by (e.g. 'Queen of Pain', 'Pudge'). "
                        "Omit to get all deaths."
                    ),
                },
            },
        },
    },
    {
        "name": "get_item_timings",
        "description": (
            "Return purchase times of key items (BKB, Blink, Aghs, etc.) for the user (default) "
            "or any specified player_slot. Use for questions about item builds, what was bought "
            "when, why an item was late/early."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_slot": {
                    "type": "integer",
                    "description": (
                        "Player slot (0-4 = Radiant, 128-132 = Dire). Omit for the user's own slot."
                    ),
                },
            },
        },
    },
    {
        "name": "get_team_stats",
        "description": (
            "Return KDA / GPM / hero damage / tower damage / net worth for all 10 players "
            "in this match, split by your team vs enemy. Use when comparing players or "
            "diagnosing team-level issues (low damage, low GPM, etc.)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recurring_patterns",
        "description": (
            "Return cross-match patterns from coach memory — which enemy heroes have killed "
            "the user repeatedly across past reviewed matches, and how many matches have been "
            "reviewed in total. Use when the user asks if a problem is recurring."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_matches",
        "description": (
            "Recent parsed matches the user played in. Optionally filter by hero they played "
            "or an enemy hero present in the match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max matches to return (default 10)."},
                "hero_name": {
                    "type": "string",
                    "description": "Filter to matches where the user played this hero.",
                },
                "vs_hero_name": {
                    "type": "string",
                    "description": "Filter to matches where this hero was on the enemy team.",
                },
            },
        },
    },
    {
        "name": "get_hero_history",
        "description": (
            "Aggregate stats across all the user's parsed matches for one specific hero — "
            "either as their own hero (as_enemy=False, default) or as a hero they faced "
            "(as_enemy=True). Returns games, wins, win-rate, and (for as_enemy) total "
            "deaths to that hero across history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hero_name": {
                    "type": "string",
                    "description": "Localized hero name (e.g. 'Storm Spirit', 'Pudge').",
                },
                "as_enemy": {
                    "type": "boolean",
                    "description": "True to query matches where this hero was an enemy; False (default) to query the user playing it.",
                },
            },
            "required": ["hero_name"],
        },
    },
]


# ─── Context (precomputed once per turn, passed to all tools) ────────────────

def _build_context(match_id: int, account_id: int) -> dict:
    """Pre-compute everything the tools need: analyze report, raw match, heroes table.

    Done once per chat turn rather than per-tool-call so a single message that
    triggers several tool calls doesn't reload the match JSON six times.
    """
    report = analyze_mod.analyze(match_id, account_id=account_id)
    match = fetcher.fetch_match(match_id)
    heroes = analyze_mod.heroes_by_id()
    you = fetcher.player_for(match, account_id)
    return {
        "match_id": match_id,
        "account_id": account_id,
        "report": report,
        "match": match,
        "heroes": heroes,
        "heroes_by_name": {h["localized_name"].lower(): h["id"] for h in heroes.values()},
        "you": you,
        "your_radiant": (you.get("player_slot") or 0) < 128,
    }


def _mmss(t: int) -> str:
    return f"{t // 60:02d}:{t % 60:02d}"


def _load_cached_match(match_id: int) -> dict | None:
    """Load a cached match JSON. Returns None if missing or unparseable."""
    cache = config.MATCHES_DIR / f"{match_id}.json"
    if not cache.exists():
        return None
    try:
        return json.loads(cache.read_text())
    except json.JSONDecodeError:
        return None


def _find_enemy_with_hero(match: dict, your_radiant: bool, hero_id: int) -> dict | None:
    """Return the enemy player who is playing hero_id, or None if no enemy has them."""
    for p in match.get("players") or []:
        is_rad = (p.get("player_slot") or 0) < 128
        if is_rad != your_radiant and p.get("hero_id") == hero_id:
            return p
    return None


# ─── Tool implementations ────────────────────────────────────────────────────

def tool_get_decision_timeline(ctx: dict) -> dict:
    decisions = ctx["report"]["decisions"]
    def _fmt(d: dict) -> dict:
        return {
            "t": d["t"],
            "type": d["type"],
            "detail": d["detail"],
            "impact_pct": round(d["impact"] * 100, 1),
        }
    return {
        "biggest_leaks": [_fmt(d) for d in decisions["biggest_leaks"]],
        "kept_doing_this": [_fmt(d) for d in decisions["kept_doing_this"]],
    }


def tool_get_player_deaths(ctx: dict, killer_hero: str | None = None) -> dict:
    you_hero_id = ctx["you"].get("hero_id")
    you_npc = ctx["heroes"].get(you_hero_id, {}).get("name", "")
    if not you_npc:
        return {"deaths": [], "count": 0, "note": "couldn't resolve user's hero in this match"}
    deaths: list[dict] = []
    for p in ctx["match"].get("players") or []:
        is_enemy = ((p.get("player_slot") or 0) < 128) != ctx["your_radiant"]
        if not is_enemy:
            continue
        killer_name = ctx["heroes"].get(p.get("hero_id"), {}).get("localized_name", "?")
        if killer_hero and killer_name.lower() != killer_hero.lower():
            continue
        for k in p.get("kills_log") or []:
            if k.get("key") == you_npc:
                deaths.append({"t": _mmss(k.get("time", 0)), "killer": killer_name})
    deaths.sort(key=lambda d: d["t"])
    return {"deaths": deaths, "count": len(deaths)}


def tool_get_item_timings(ctx: dict, player_slot: int | None = None) -> dict:
    if player_slot is not None:
        player = next(
            (p for p in ctx["match"].get("players") or []
             if p.get("player_slot") == player_slot),
            None,
        )
    else:
        player = ctx["you"]
    if not player:
        return {"items": [], "note": "no player at that slot"}
    items: list[dict] = []
    for it in player.get("purchase_log") or []:
        key = it.get("key", "")
        if key in analyze_mod.KEY_ITEMS:
            items.append({"item": analyze_mod.KEY_ITEMS[key], "time": _mmss(it["time"])})
    return {
        "hero": ctx["heroes"].get(player.get("hero_id"), {}).get("localized_name", "?"),
        "items": items,
    }


def tool_get_team_stats(ctx: dict) -> dict:
    teams: dict[str, list[dict]] = {"your_team": [], "enemy": []}
    for p in ctx["match"].get("players") or []:
        is_radiant = (p.get("player_slot") or 0) < 128
        side = "your_team" if is_radiant == ctx["your_radiant"] else "enemy"
        teams[side].append({
            "hero": ctx["heroes"].get(p.get("hero_id"), {}).get("localized_name", "?"),
            "kda": f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)}",
            "gpm": p.get("gold_per_min"),
            "xpm": p.get("xp_per_min"),
            "net_worth": p.get("net_worth"),
            "hero_damage": p.get("hero_damage"),
            "tower_damage": p.get("tower_damage"),
            "is_user": p.get("account_id") == ctx["account_id"],
            "player_slot": p.get("player_slot"),
        })
    return teams


def tool_get_recurring_patterns(ctx: dict) -> dict:
    mem = coach_mod._load_memory(ctx["account_id"])
    history = mem.get("history") or []
    if not history:
        return {"patterns": [], "reviewed_matches": 0,
                "note": "coach memory empty — generate some coach reviews first"}
    death_counts: Counter = Counter()
    for h in history:
        for theme in h.get("themes") or []:
            if theme.startswith("repeat-deaths:"):
                death_counts[theme.split(":", 1)[1]] += 1
    return {
        "reviewed_matches": len(history),
        "killer_patterns": [
            {"killer": name, "matches_killed_in": count}
            for name, count in death_counts.most_common(8) if count >= 2
        ],
        "recent_reviews": [
            {"date": h.get("date"), "hero": h.get("hero"), "result": h.get("result"),
             "kda": h.get("kda"), "themes": h.get("themes") or []}
            for h in history[-5:]
        ],
    }


def tool_get_recent_matches(
    ctx: dict, limit: int = 10,
    hero_name: str | None = None, vs_hero_name: str | None = None,
) -> dict:
    aid = ctx["account_id"]
    by_name = ctx["heroes_by_name"]
    hero_id = by_name.get((hero_name or "").lower()) if hero_name else None
    vs_hero_id = by_name.get((vs_hero_name or "").lower()) if vs_hero_name else None

    if hero_name and not hero_id:
        return {"matches": [], "error": f"unknown hero: {hero_name}"}
    if vs_hero_name and not vs_hero_id:
        return {"matches": [], "error": f"unknown enemy hero: {vs_hero_name}"}

    # Pull a generous candidate set when we'll filter further in Python.
    db_limit = max(limit * 5, limit) if vs_hero_id else limit
    where = "um.user_id = %s AND m.parsed = true"
    params: list = [aid]
    if hero_id:
        where += " AND um.hero_id = %s"
        params.append(hero_id)
    params.append(db_limit)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT um.match_id, um.hero_id, um.kills, um.deaths, um.assists, "
            "um.slot, m.radiant_win, m.duration "
            f"FROM user_matches um JOIN matches m ON m.match_id = um.match_id "
            f"WHERE {where} ORDER BY m.start_time DESC LIMIT %s",
            tuple(params),
        ).fetchall()

    results: list[dict] = []
    for mid, hid, k, d, a, slot, radiant_win, dur in rows:
        your_radiant = (slot or 0) < 128
        if vs_hero_id:
            m = _load_cached_match(mid)
            if not m or not _find_enemy_with_hero(m, your_radiant, vs_hero_id):
                continue
        results.append({
            "match_id": mid,
            "hero": ctx["heroes"].get(hid, {}).get("localized_name", "?"),
            "kda": f"{k}/{d}/{a}",
            "result": "win" if your_radiant == radiant_win else "loss",
            "duration_min": (dur or 0) // 60,
        })
        if len(results) >= limit:
            break

    return {"matches": results, "count": len(results)}


def tool_get_hero_history(ctx: dict, hero_name: str, as_enemy: bool = False) -> dict:
    hero_id = ctx["heroes_by_name"].get(hero_name.lower())
    if not hero_id:
        return {"error": f"unknown hero: {hero_name}"}
    aid = ctx["account_id"]

    if not as_enemy:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT um.kills, um.deaths, um.assists, um.slot, m.radiant_win "
                "FROM user_matches um JOIN matches m ON m.match_id = um.match_id "
                "WHERE um.user_id = %s AND um.hero_id = %s AND m.parsed = true",
                (aid, hero_id),
            ).fetchall()
        n = len(rows)
        if n == 0:
            return {"hero": hero_name, "as": "your_hero", "games": 0,
                    "note": "no parsed matches with you on this hero"}
        wins = sum(1 for _k, _d, _a, slot, rw in rows if ((slot or 0) < 128) == rw)
        def _avg(i: int) -> float:
            return round(sum(r[i] for r in rows) / n, 1)
        return {
            "hero": hero_name, "as": "your_hero",
            "games": n, "wins": wins, "win_rate_pct": round(wins / n * 100, 1),
            "avg_kills": _avg(0), "avg_deaths": _avg(1), "avg_assists": _avg(2),
        }

    # as_enemy=True: scan recent parsed matches for ones where this hero was on the enemy team.
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT um.match_id, um.slot, m.radiant_win "
            "FROM user_matches um JOIN matches m ON m.match_id = um.match_id "
            "WHERE um.user_id = %s AND m.parsed = true "
            "ORDER BY m.start_time DESC LIMIT 50",
            (aid,),
        ).fetchall()

    n = wins = total_deaths = 0
    for mid, slot, radiant_win in rows:
        m = _load_cached_match(mid)
        if not m:
            continue
        your_radiant = (slot or 0) < 128
        enemy = _find_enemy_with_hero(m, your_radiant, hero_id)
        if not enemy:
            continue
        n += 1
        if your_radiant == radiant_win:
            wins += 1
        you = fetcher.player_for(m, aid)
        you_npc = ctx["heroes"].get(you.get("hero_id"), {}).get("name", "") if you else ""
        if you_npc:
            total_deaths += sum(
                1 for k in (enemy.get("kills_log") or []) if k.get("key") == you_npc
            )

    if n == 0:
        return {"hero": hero_name, "as": "enemy", "games_faced": 0,
                "note": "no parsed matches found where you faced this hero"}
    return {
        "hero": hero_name, "as": "enemy",
        "games_faced": n, "wins": wins,
        "win_rate_pct": round(wins / n * 100, 1),
        "total_deaths_to_them": total_deaths,
        "avg_deaths_per_match": round(total_deaths / n, 2),
    }


TOOL_DISPATCH = {
    "get_decision_timeline": tool_get_decision_timeline,
    "get_player_deaths": tool_get_player_deaths,
    "get_item_timings": tool_get_item_timings,
    "get_team_stats": tool_get_team_stats,
    "get_recurring_patterns": tool_get_recurring_patterns,
    "get_recent_matches": tool_get_recent_matches,
    "get_hero_history": tool_get_hero_history,
}


# ─── Persistence ─────────────────────────────────────────────────────────────

def load_history(account_id: int, match_id: int) -> list[dict]:
    """Load persisted chat turns (newest-last). Each entry is {role, content, ts, ...}."""
    path = config.chats_dir_for(account_id) / f"{match_id}.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_history(
    account_id: int, match_id: int,
    user_text: str, assistant_text: str,
    cost_cents: int, tool_calls: list[dict],
) -> None:
    path = config.chats_dir_for(account_id) / f"{match_id}.jsonl"
    now = time.time()
    with path.open("a") as f:
        f.write(json.dumps({"role": "user", "content": user_text, "ts": now}) + "\n")
        f.write(json.dumps({
            "role": "assistant", "content": assistant_text, "ts": now,
            "cost_cents": cost_cents, "tool_calls": tool_calls,
        }) + "\n")


def clear_history(account_id: int, match_id: int) -> None:
    path = config.chats_dir_for(account_id) / f"{match_id}.jsonl"
    path.unlink(missing_ok=True)


# ─── Chat loop ───────────────────────────────────────────────────────────────

def _match_context_block(ctx: dict) -> str:
    """Compact match summary injected into the system message — basic facts so the model
    doesn't need to call tools for every chat just to know what happened."""
    report = ctx["report"]
    you = report["you"]
    return (
        "Current match being discussed:\n"
        f"- Match ID: {ctx['match_id']}\n"
        f"- You: {you['hero']} (lane {you.get('lane_role','?')}), KDA {you['kda']}, on {you['team']}\n"
        f"- Result: {you['result'].upper()} in {report['duration_min']} min\n"
        f"- {len(report['decisions']['biggest_leaks'])} leaks above threshold, "
        f"{len(report['decisions']['kept_doing_this'])} good plays above threshold."
    )


def _history_for_api(history: list[dict]) -> list[dict]:
    """Convert persisted history to the Anthropic messages format.

    We only keep the last CHAT_HISTORY_TURNS pairs to bound prompt size — older
    turns are still on disk for the user to scroll through, just not replayed
    to the model on every turn.
    """
    pairs: list[tuple[dict, dict | None]] = []
    pending_user: dict | None = None
    for h in history:
        if h.get("role") == "user":
            pending_user = h
        elif h.get("role") == "assistant" and pending_user is not None:
            pairs.append((pending_user, h))
            pending_user = None
    pairs = pairs[-CHAT_HISTORY_TURNS:]
    out: list[dict] = []
    for u, a in pairs:
        out.append({"role": "user", "content": u["content"]})
        out.append({"role": "assistant", "content": a["content"]})
    return out


def chat_turn(
    match_id: int,
    account_id: int,
    user_message: str,
    history: list[dict] | None = None,
    model: str = "haiku",
) -> dict:
    """Run one chat turn — tool-use loop until end_turn, capped at CHAT_MAX_TOOL_TURNS.

    Returns: {assistant, tool_calls, cost_cents, usage, model, tool_cap_hit}
    Raises: SystemExit on Anthropic auth/rate/API errors, BudgetExceeded if capped.
    """
    api_key, use_byo = coach_mod._resolve_api_key(account_id)
    cost.check_budget(account_id, use_byo)

    ctx = _build_context(match_id, account_id)
    system = CHAT_SYSTEM_PROMPT + "\n\n" + _match_context_block(ctx)

    messages: list[dict] = _history_for_api(history or [])
    messages.append({"role": "user", "content": user_message})

    model_id = coach_mod.MODEL_ALIASES.get(model, model)
    client = anthropic.Anthropic(api_key=api_key)

    total_usage = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    tool_calls_log: list[dict] = []

    def _finalize(text: str, tool_cap_hit: bool = False) -> dict:
        cents = cost.estimate_cents(model_id, total_usage)
        cost.charge(account_id, cents, use_byo_key=use_byo)
        return {
            "assistant": text,
            "tool_calls": tool_calls_log,
            "cost_cents": cents,
            "usage": total_usage,
            "model": model_id,
            "tool_cap_hit": tool_cap_hit,
        }

    for turn_n in range(CHAT_MAX_TOOL_TURNS + 1):
        try:
            resp = client.messages.create(
                model=model_id,
                max_tokens=CHAT_MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.AuthenticationError as e:
            raise SystemExit(f"Anthropic authentication failed: {e.message}")
        except anthropic.RateLimitError as e:
            raise SystemExit(f"Rate limited by Anthropic: {e.message}")
        except anthropic.APIStatusError as e:
            raise SystemExit(f"Anthropic API error ({e.status_code}): {e.message}")

        total_usage["input_tokens"] += resp.usage.input_tokens
        total_usage["output_tokens"] += resp.usage.output_tokens
        total_usage["cache_read_input_tokens"] += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        total_usage["cache_creation_input_tokens"] += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0

        text = "".join(b.text for b in resp.content if b.type == "text").strip()

        if resp.stop_reason != "tool_use":
            return _finalize(text or "(narrator went uncharacteristically silent)")

        if turn_n == CHAT_MAX_TOOL_TURNS:
            return _finalize(
                text or "The narrator exceeded the data-gathering budget mid-thought. "
                       "Try a more specific question.",
                tool_cap_hit=True,
            )

        # Echo the assistant's tool-use turn back, then resolve tools.
        messages.append({"role": "assistant", "content": resp.content})
        tool_results: list[dict] = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            name = block.name
            inp = dict(block.input or {})
            tool_calls_log.append({"name": name, "input": inp})
            try:
                fn = TOOL_DISPATCH.get(name)
                result = fn(ctx, **inp) if fn else {"error": f"unknown tool: {name}"}
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    # Should be unreachable — the loop returns in every iteration.
    return _finalize("(narrator lost the thread)", tool_cap_hit=True)


def chat(
    match_id: int,
    account_id: int,
    user_message: str,
    model: str = "haiku",
    persist: bool = True,
) -> dict:
    """Convenience wrapper: load history → run turn → append result to disk."""
    history = load_history(account_id, match_id) if persist else []
    result = chat_turn(match_id, account_id, user_message, history=history, model=model)
    if persist:
        append_history(
            account_id, match_id,
            user_text=user_message,
            assistant_text=result["assistant"],
            cost_cents=result["cost_cents"],
            tool_calls=result["tool_calls"],
        )
    return result
