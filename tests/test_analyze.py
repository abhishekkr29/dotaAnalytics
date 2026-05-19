from app import analyze
from tests.conftest import synthetic_match


def test_format_time():
    assert analyze._format_time(0) == "00:00"
    assert analyze._format_time(75) == "01:15"
    assert analyze._format_time(3600) == "60:00"
    assert analyze._format_time(-30) == "-00:30"


def test_replay_url_uses_dota2_scheme():
    url = analyze._replay_url(8807224804, 900)
    assert url == "dota2://matchid=8807224804&matchtime=900"


def test_replay_url_clamps_negative_time():
    url = analyze._replay_url(1, -10)
    assert "matchtime=0" in url


def test_cluster_deaths_merges_same_fight():
    deaths = [
        {"t": 500, "type": "death", "detail": "Died to Pudge"},
        {"t": 510, "type": "death", "detail": "Died to Lina"},   # within 30s → same fight
        {"t": 525, "type": "death", "detail": "Died to Pudge"},  # within 30s of Lina
        {"t": 800, "type": "death", "detail": "Died to Pudge"},  # separate fight
    ]
    clustered = analyze._cluster_deaths(deaths, window_s=30)
    deaths_out = [d for d in clustered if d["type"] == "death"]
    assert len(deaths_out) == 2
    cluster = next(d for d in deaths_out if d.get("cluster_size"))
    assert cluster["cluster_size"] == 3
    assert "Pudge" in cluster["detail"]
    assert "Lina" in cluster["detail"]


def test_cluster_deaths_preserves_non_deaths():
    decisions = [
        {"t": 100, "type": "item", "detail": "Bought BKB"},
        {"t": 500, "type": "death", "detail": "Died to Pudge"},
        {"t": 510, "type": "death", "detail": "Died to Lina"},
        {"t": 700, "type": "kill", "detail": "Killed Rubick"},
    ]
    out = analyze._cluster_deaths(decisions, window_s=30)
    types = sorted(d["type"] for d in out)
    assert types == ["death", "item", "kill"]  # 2 deaths collapsed to 1


def test_extract_decisions_yields_item_death_kill_roshan():
    match = synthetic_match()
    you = match["players"][0]  # the Storm Spirit at slot 0
    heroes = {
        74: {"name": "npc_dota_hero_invoker",      "localized_name": "Invoker"},
        10: {"name": "npc_dota_hero_morphling",    "localized_name": "Morphling"},
        14: {"name": "npc_dota_hero_pudge",        "localized_name": "Pudge"},
        16: {"name": "npc_dota_hero_sand_king",    "localized_name": "Sand King"},
    }
    # we don't actually call _extract_decisions with player's true hero npc, so
    # pretend the Storm Spirit player has hero_id 74 (Invoker) for this test —
    # the function uses heroes[you.hero_id].name to look up the kill log key.
    # The synthetic fixture uses kill log key "npc_dota_hero_storm_spirit"
    # which corresponds to hero_id 74 in the real data — we map it accordingly here.
    you = match["players"][0]
    you["hero_id"] = 74
    heroes[74] = {"name": "npc_dota_hero_storm_spirit", "localized_name": "Storm Spirit"}
    heroes_by_npc = {h["name"]: h for h in heroes.values()}

    decisions = analyze._extract_decisions(
        match, you, heroes, heroes_by_npc, user_team_is_radiant=True
    )
    types = {d["type"] for d in decisions}
    assert "item" in types     # BKB, Blink in purchase_log
    assert "death" in types    # Pudge killed Storm twice
    assert "kill" in types     # Storm killed Pudge once
    assert "roshan" in types   # objectives has a roshan kill by Radiant


def test_filter_drops_death_with_positive_impact():
    """A death with positive Δwp is a co-event, not a cause. Drop it."""
    decisions = [
        {"t": 600, "type": "death", "detail": "Died to Pudge", "impact": 0.05},
    ]
    out = analyze._filter_implausible_attributions(decisions)
    assert out == []


def test_filter_drops_kill_with_negative_impact():
    """A kill with negative Δwp is a co-event, not a cause. Drop it."""
    decisions = [
        {"t": 600, "type": "kill", "detail": "Killed Pudge", "impact": -0.05},
    ]
    out = analyze._filter_implausible_attributions(decisions)
    assert out == []


def test_filter_drops_roshan_with_negative_impact():
    decisions = [
        {"t": 900, "type": "roshan", "detail": "Your team killed Roshan", "impact": -0.03},
    ]
    out = analyze._filter_implausible_attributions(decisions)
    assert out == []


def test_filter_drops_item_near_death():
    """Item bought right after a death gets dropped — the death is the real cause of the Δwp drop."""
    decisions = [
        {"t": 600, "type": "death", "detail": "Died to Pudge", "impact": -0.184},
        {"t": 650, "type": "item",  "detail": "Bought Spirit Vessel", "impact": -0.184},
    ]
    out = analyze._filter_implausible_attributions(decisions)
    types = [d["type"] for d in out]
    assert "death" in types
    assert "item" not in types


def test_filter_drops_ward_near_kill():
    """Sentry placed near a kill gets dropped — the kill is the cause of the Δwp rise."""
    decisions = [
        {"t": 1200, "type": "kill",     "detail": "Killed Lina",    "impact":  0.087},
        {"t": 1220, "type": "ward_sen", "detail": "Placed sentry",  "impact":  0.087},
    ]
    out = analyze._filter_implausible_attributions(decisions)
    types = [d["type"] for d in out]
    assert "kill" in types
    assert "ward_sen" not in types


def test_filter_keeps_isolated_item():
    """An item with negative Δwp that's NOT near a death stays (could be a real timing leak)."""
    decisions = [
        {"t": 600, "type": "item", "detail": "Bought BKB", "impact": -0.04},
    ]
    out = analyze._filter_implausible_attributions(decisions)
    assert len(out) == 1
    assert out[0]["type"] == "item"


def test_filter_keeps_death_with_negative_impact_and_drops_co_event():
    """The classic case: 5-event cluster reduces to just the death/kill that caused it."""
    decisions = [
        {"t":  390, "type": "death",    "detail": "Died to Shaman",      "impact": -0.184},
        {"t":  410, "type": "item",     "detail": "Bought Spirit Vessel", "impact": -0.184},
        {"t":  856, "type": "item",     "detail": "Bought Blink",        "impact": -0.137},
        {"t":  810, "type": "ward_sen", "detail": "Placed sentry",       "impact": -0.137},
        {"t":  844, "type": "death",    "detail": "Died to QoP",         "impact": -0.137},
    ]
    out = analyze._filter_implausible_attributions(decisions)
    # Both deaths survive; both items and the ward are dropped (co-events)
    deaths_in = sum(1 for d in out if d["type"] == "death")
    items_in  = sum(1 for d in out if d["type"] == "item")
    wards_in  = sum(1 for d in out if d["type"] == "ward_sen")
    assert deaths_in == 2
    assert items_in == 0
    assert wards_in == 0


def test_extract_decisions_includes_buybacks():
    match = synthetic_match()
    you = match["players"][0]
    you["buyback_log"] = [{"time": 1100}]
    you["hero_id"] = 74
    heroes = {74: {"name": "npc_dota_hero_storm_spirit", "localized_name": "Storm Spirit"}}
    decisions = analyze._extract_decisions(match, you, heroes, {}, user_team_is_radiant=True)
    bb = [d for d in decisions if d["type"] == "buyback"]
    assert len(bb) == 1
    assert bb[0]["t"] == 1100
