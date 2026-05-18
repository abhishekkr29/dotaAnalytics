from app import snapshots
from tests.conftest import synthetic_match


def test_extract_returns_one_row_per_minute():
    match = synthetic_match()
    snaps = snapshots.extract(match)
    assert len(snaps) == len(match["radiant_gold_adv"])
    assert all(snaps[i]["minute"] == i for i in range(len(snaps)))


def test_extract_carries_avg_rank_and_radiant_win():
    match = synthetic_match()
    snaps = snapshots.extract(match)
    assert snaps[0]["radiant_win"] is True
    # avg_rank_tier comes from the players' rank_tier values
    assert snaps[0]["avg_rank_tier"] is not None
    assert 40 <= snaps[0]["avg_rank_tier"] <= 50


def test_extract_returns_empty_for_unparsed():
    match = synthetic_match()
    match["version"] = None  # mark as not parsed
    assert snapshots.extract(match) == []


def test_extract_tower_kills_are_cumulative():
    match = synthetic_match()
    # Add a second tower kill 5 min later
    match["objectives"].append(
        {"time": 540, "type": "CHAT_MESSAGE_TOWER_KILL", "team": 2}
    )
    snaps = snapshots.extract(match)
    # cumulative: dire tower count should never decrease
    counts = [s["tower_kills_dire"] for s in snaps]
    assert counts == sorted(counts)
    assert counts[-1] >= 2  # at least the two tower kills we added (both team=2 → dire side)


def test_hero_compositions_are_sorted_for_stability():
    match = synthetic_match()
    snaps = snapshots.extract(match)
    r_heroes = [snaps[0][f"r_hero_{i}"] for i in range(1, 6)]
    d_heroes = [snaps[0][f"d_hero_{i}"] for i in range(1, 6)]
    assert r_heroes == sorted(r_heroes)
    assert d_heroes == sorted(d_heroes)
