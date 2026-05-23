"""Tests for the chat agent — tool dispatch + persistence.

No Anthropic calls — only the tool implementations + history I/O. The
tool functions all take a ctx dict so we can synthesize ctx directly
without going through analyze_mod (which requires a trained model).
"""

from app import chat as chat_mod
from tests.conftest import synthetic_match


def _fake_heroes() -> dict:
    """Subset of the OpenDota /heroes payload — just the hero_ids used in the
    synthetic match, mapped to their canonical npc names + localized names."""
    raw = {
        74: ("npc_dota_hero_invoker",       "Invoker"),
        10: ("npc_dota_hero_morphling",     "Morphling"),
        5:  ("npc_dota_hero_crystal_maiden","Crystal Maiden"),
        7:  ("npc_dota_hero_earthshaker",   "Earthshaker"),
        8:  ("npc_dota_hero_juggernaut",    "Juggernaut"),
        14: ("npc_dota_hero_pudge",         "Pudge"),
        16: ("npc_dota_hero_sand_king",     "Sand King"),
        20: ("npc_dota_hero_vengefulspirit","Vengeful Spirit"),
        26: ("npc_dota_hero_lion",          "Lion"),
        30: ("npc_dota_hero_witch_doctor",  "Witch Doctor"),
    }
    return {hid: {"id": hid, "name": npc, "localized_name": loc}
            for hid, (npc, loc) in raw.items()}


def _build_ctx() -> dict:
    """Synthesize a chat ctx without going through analyze() — enough for tool tests."""
    match = synthetic_match()
    heroes = _fake_heroes()
    # User is player_slot 0, Storm Spirit-ish role; conftest uses hero_id 74 = Invoker per our map.
    account_id = 446619601
    you = next(p for p in match["players"] if p["account_id"] == account_id)
    # Fake the user's hero NPC name to match the synthetic kills_log keys ("storm_spirit").
    heroes[74] = {"id": 74, "name": "npc_dota_hero_storm_spirit", "localized_name": "Storm Spirit"}
    # Minimal fake report — only the bits the timeline tool reads.
    report = {
        "you": {
            "hero": "Storm Spirit", "team": "radiant", "kda": "5/2/8",
            "result": "win", "lane_role": "mid",
        },
        "duration_min": 20,
        "decisions": {
            "biggest_leaks": [
                {"t": "08:30", "type": "death", "detail": "Died to Pudge",
                 "impact": -0.082, "replay_url": "dota2://..."},
            ],
            "kept_doing_this": [
                {"t": "12:00", "type": "kill", "detail": "Killed Pudge",
                 "impact": 0.061, "replay_url": "dota2://..."},
            ],
        },
    }
    return {
        "match_id": match["match_id"],
        "account_id": account_id,
        "report": report,
        "match": match,
        "heroes": heroes,
        "heroes_by_name": {h["localized_name"].lower(): h["id"] for h in heroes.values()},
        "you": you,
        "your_radiant": True,
    }


# ── Tool: get_player_deaths ──────────────────────────────────────────────────

def test_get_player_deaths_returns_all_when_unfiltered():
    ctx = _build_ctx()
    res = chat_mod.tool_get_player_deaths(ctx)
    # Synthetic match has Pudge killing the user twice.
    assert res["count"] == 2
    assert all(d["killer"] == "Pudge" for d in res["deaths"])
    assert {d["t"] for d in res["deaths"]} == {"08:30", "08:40"}


def test_get_player_deaths_filters_by_killer_hero():
    ctx = _build_ctx()
    res = chat_mod.tool_get_player_deaths(ctx, killer_hero="Queen of Pain")
    assert res["count"] == 0
    res = chat_mod.tool_get_player_deaths(ctx, killer_hero="Pudge")
    assert res["count"] == 2


def test_get_player_deaths_killer_filter_is_case_insensitive():
    ctx = _build_ctx()
    res = chat_mod.tool_get_player_deaths(ctx, killer_hero="pUdGe")
    assert res["count"] == 2


# ── Tool: get_decision_timeline ──────────────────────────────────────────────

def test_get_decision_timeline_returns_leaks_and_kept():
    ctx = _build_ctx()
    res = chat_mod.tool_get_decision_timeline(ctx)
    assert len(res["biggest_leaks"]) == 1
    assert res["biggest_leaks"][0]["detail"].startswith("Died to")
    assert res["biggest_leaks"][0]["impact_pct"] == -8.2   # rounded from -0.082
    assert res["kept_doing_this"][0]["impact_pct"] == 6.1


# ── Tool: get_item_timings ───────────────────────────────────────────────────

def test_get_item_timings_returns_user_key_items():
    ctx = _build_ctx()
    res = chat_mod.tool_get_item_timings(ctx)
    items = {i["item"] for i in res["items"]}
    # Synthetic user bought Blink + BKB
    assert "Blink Dagger" in items
    assert "BKB" in items


def test_get_item_timings_handles_other_player_slot():
    ctx = _build_ctx()
    # Slot 1 has empty purchase_log in the synthetic match — should return [].
    res = chat_mod.tool_get_item_timings(ctx, player_slot=1)
    assert res["items"] == []


# ── Tool: get_team_stats ─────────────────────────────────────────────────────

def test_get_team_stats_splits_by_side():
    ctx = _build_ctx()
    res = chat_mod.tool_get_team_stats(ctx)
    assert len(res["your_team"]) == 5
    assert len(res["enemy"]) == 5
    you = next(p for p in res["your_team"] if p["is_user"])
    assert you["kda"] == "5/2/8"
    assert all(not p["is_user"] for p in res["enemy"])


# ── History persistence ─────────────────────────────────────────────────────

def test_history_roundtrip(tmp_path, monkeypatch):
    # Redirect CHATS_DIR to tmp_path for hermetic test
    monkeypatch.setattr(chat_mod.config, "CHATS_DIR", tmp_path)
    monkeypatch.setattr(chat_mod.config, "chats_dir_for", lambda aid: tmp_path / str(aid))
    (tmp_path / "1").mkdir(parents=True, exist_ok=True)

    aid, mid = 1, 999
    chat_mod.append_history(aid, mid, "why did I die so much?", "Eleven times. Eleven.",
                             cost_cents=2, tool_calls=[{"name": "get_player_deaths", "input": {}}])
    chat_mod.append_history(aid, mid, "and to whom?", "Pudge, mostly.",
                             cost_cents=1, tool_calls=[])

    loaded = chat_mod.load_history(aid, mid)
    assert len(loaded) == 4
    assert loaded[0]["role"] == "user" and loaded[0]["content"] == "why did I die so much?"
    assert loaded[1]["role"] == "assistant" and loaded[1]["cost_cents"] == 2
    assert loaded[2]["content"] == "and to whom?"
    assert loaded[3]["content"] == "Pudge, mostly."


def test_clear_history_wipes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_mod.config, "CHATS_DIR", tmp_path)
    monkeypatch.setattr(chat_mod.config, "chats_dir_for", lambda aid: tmp_path / str(aid))
    (tmp_path / "1").mkdir(parents=True, exist_ok=True)

    aid, mid = 1, 999
    chat_mod.append_history(aid, mid, "q", "a", cost_cents=1, tool_calls=[])
    assert chat_mod.load_history(aid, mid)

    chat_mod.clear_history(aid, mid)
    assert chat_mod.load_history(aid, mid) == []


def test_history_for_api_pairs_and_trims():
    """The API-formatted history should be alternating user/assistant pairs,
    trimmed to CHAT_HISTORY_TURNS most-recent pairs, with metadata stripped."""
    hist = []
    for i in range(15):
        hist.append({"role": "user", "content": f"q{i}", "ts": i})
        hist.append({"role": "assistant", "content": f"a{i}", "ts": i, "cost_cents": 1})
    out = chat_mod._history_for_api(hist)
    # 15 pairs -> trimmed to last CHAT_HISTORY_TURNS pairs * 2 messages each
    assert len(out) == chat_mod.CHAT_HISTORY_TURNS * 2
    # Latest pair preserved
    assert out[-2] == {"role": "user", "content": "q14"}
    assert out[-1] == {"role": "assistant", "content": "a14"}
    # No leaked metadata
    assert "ts" not in out[0]
    assert "cost_cents" not in out[-1]


def test_history_for_api_skips_orphan_assistant():
    """A stray assistant without a preceding user is dropped (defensive)."""
    hist = [
        {"role": "assistant", "content": "orphan", "ts": 0},
        {"role": "user", "content": "q", "ts": 1},
        {"role": "assistant", "content": "a", "ts": 1},
    ]
    out = chat_mod._history_for_api(hist)
    assert out == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
