"""Tests for the blame feature — Stanley Parable narrator scapegoating.

No Anthropic calls — only the picker + prompt-builder logic.
"""

from app import coach


def _five_player_team(radiant: bool, kdas: list[tuple[int, int, int]], gpms: list[int],
                       account_ids: list[int], hero_ids: list[int],
                       hero_dmgs: list[int] | None = None,
                       tower_dmgs: list[int] | None = None) -> list[dict]:
    base = 0 if radiant else 128
    hero_dmgs = hero_dmgs or [g * 50 for g in gpms]   # ~scale with GPM by default
    tower_dmgs = tower_dmgs or [g * 5 for g in gpms]
    out = []
    for i, ((k, d, a), gpm, aid, hid) in enumerate(zip(kdas, gpms, account_ids, hero_ids)):
        out.append({
            "player_slot": base + i,
            "account_id": aid,
            "hero_id": hid,
            "kills": k, "deaths": d, "assists": a,
            "gold_per_min": gpm, "xp_per_min": gpm + 50,
            "net_worth": gpm * 30, "last_hits": 100, "denies": 5,
            "hero_damage": hero_dmgs[i],
            "tower_damage": tower_dmgs[i],
            "lane_role": 1 if i == 0 else (2 if i == 1 else (3 if i == 2 else (4 if i == 3 else 5))),
            "purchase_log": [], "kills_log": [],
            "obs_log": [], "sen_log": [], "buyback_log": [],
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# _infer_role — derives role from GPM rank within team

def test_infer_role_by_gpm_rank():
    team = _five_player_team(
        radiant=True,
        kdas=[(0, 0, 0)] * 5,
        gpms=[700, 600, 450, 350, 250],
        account_ids=[1, 2, 3, 4, 5],
        hero_ids=[10, 11, 12, 13, 14],
    )
    assert coach._infer_role(team[0], team) == "carry"   # highest GPM
    assert coach._infer_role(team[1], team) == "mid"
    assert coach._infer_role(team[2], team) == "off"
    assert coach._infer_role(team[3], team) == "sup4"
    assert coach._infer_role(team[4], team) == "sup5"    # lowest GPM


# ─────────────────────────────────────────────────────────────────────────────
# _blame_score — z-score composite

def test_blame_score_penalizes_excess_deaths_for_role():
    """A carry baseline is 4 deaths. 8 deaths = +100% over baseline = high blame factor."""
    carry = {"deaths": 8, "gold_per_min": 600, "hero_damage": 25000, "tower_damage": 5000}
    score, factors = coach._blame_score(carry, "carry")
    assert factors["deaths"]["z"] >= 0.9, "should flag 8 deaths as ~2x baseline"
    assert score > 0   # composite is positive (player is blameable on at least one axis)


def test_blame_score_rewards_meeting_baseline():
    """A support hitting baseline for everything has a low composite."""
    sup = {"deaths": 10, "gold_per_min": 320, "hero_damage": 14000, "tower_damage": 1200}
    score, factors = coach._blame_score(sup, "sup5")
    for f in factors.values():
        assert abs(f["z"]) < 0.01    # all factors ~0


def test_carry_with_8_deaths_beats_support_with_12_deaths():
    """The key fairness check: role normalization makes a deviation-from-baseline
    matter more than raw count. Carry's +100% deaths > support's +20% deaths."""
    team = _five_player_team(
        radiant=True,
        # Carry: 8 deaths (vs 4 baseline = +100%). Support: 12 deaths (vs 10 baseline = +20%).
        kdas=[(5, 8, 8), (6, 4, 9), (5, 5, 11), (3, 7, 12), (1, 12, 14)],
        gpms=[600, 580, 460, 380, 320],   # matching role baselines so deaths dominate
        account_ids=[101, 102, 103, 104, 105],
        hero_ids=[10, 11, 12, 13, 14],
    )
    match = {"match_id": 1, "duration": 1800, "radiant_win": False,
             "players": team + _five_player_team(False, [(0,0,0)]*5, [500]*5,
                                                 [201,202,203,204,205], [15,16,17,18,19])}
    target, role, factors = coach._pick_blame_target(match, losing_team_radiant=True,
                                                      exclude_account=None)
    # Carry has +100% deaths, support has +20%. Carry should be picked despite fewer raw deaths.
    assert target["account_id"] == 101, "carry's role-relative excess should beat support's raw count"
    assert role == "carry"


def test_support_with_high_deaths_still_picked_if_excess_is_huge():
    """Support with 20 deaths (2x baseline) DOES still get picked over a healthy carry."""
    team = _five_player_team(
        radiant=True,
        kdas=[(5, 4, 8), (6, 4, 9), (5, 5, 11), (3, 7, 12), (1, 20, 14)],   # sup has 20 deaths
        gpms=[600, 580, 460, 380, 320],
        account_ids=[101, 102, 103, 104, 105],
        hero_ids=[10, 11, 12, 13, 14],
    )
    match = {"match_id": 1, "duration": 1800, "radiant_win": False,
             "players": team + _five_player_team(False, [(0,0,0)]*5, [500]*5,
                                                 [201,202,203,204,205], [15,16,17,18,19])}
    target, role, _ = coach._pick_blame_target(match, losing_team_radiant=True,
                                                exclude_account=None)
    assert target["account_id"] == 105
    assert role == "sup5"


def test_pick_excludes_user_by_default():
    """When the user is the worst, excluding self picks the next worst."""
    team = _five_player_team(
        radiant=True,
        kdas=[(5, 8, 8), (6, 4, 9), (5, 5, 11), (3, 7, 12), (1, 12, 14)],
        gpms=[600, 580, 460, 380, 320],
        # User is the carry at idx 0
        account_ids=[446619601, 102, 103, 104, 105],
        hero_ids=[10, 11, 12, 13, 14],
    )
    match = {"match_id": 1, "duration": 1800, "radiant_win": False,
             "players": team + _five_player_team(False, [(0,0,0)]*5, [500]*5,
                                                 [201,202,203,204,205], [15,16,17,18,19])}
    target, _role, _factors = coach._pick_blame_target(
        match, losing_team_radiant=True, exclude_account=446619601,
    )
    assert target["account_id"] != 446619601


def test_pick_only_from_losing_team():
    radiant = _five_player_team(
        radiant=True, kdas=[(10, 1, 5)] * 5, gpms=[700] * 5,
        account_ids=list(range(101, 106)), hero_ids=[10, 11, 12, 13, 14],
    )
    dire = _five_player_team(
        radiant=False, kdas=[(2, 12, 5)] * 5, gpms=[300] * 5,
        account_ids=list(range(201, 206)), hero_ids=[15, 16, 17, 18, 19],
    )
    match = {"match_id": 1, "duration": 1800, "radiant_win": True, "players": radiant + dire}
    target, _role, _f = coach._pick_blame_target(match, losing_team_radiant=False,
                                                  exclude_account=None)
    is_radiant = (target.get("player_slot") or 0) < 128
    assert is_radiant is False


def test_pick_blame_targets_enemy_on_win():
    """When the user won, the picker must operate on the ENEMY (losing) team."""
    radiant = _five_player_team(
        radiant=True, kdas=[(15, 1, 10)] * 5, gpms=[600] * 5,
        account_ids=[446619601] + [102, 103, 104, 105], hero_ids=[74, 14, 8, 30, 5],
    )
    dire = _five_player_team(
        radiant=False,
        kdas=[(2, 18, 5), (3, 6, 7), (4, 7, 9), (5, 8, 6), (1, 12, 3)],
        gpms=[200, 350, 380, 400, 280],
        account_ids=[201, 202, 203, 204, 205], hero_ids=[10, 11, 12, 13, 15],
    )
    match = {"match_id": 1, "duration": 1800, "radiant_win": True, "players": radiant + dire}
    target, role, _f = coach._pick_blame_target(match, losing_team_radiant=False,
                                                 exclude_account=None)
    # Critical invariant: must be on the losing (dire) team, never radiant.
    assert (target["player_slot"] or 0) >= 128
    # Role-normalized: carry-class (highest-GPM on dire = 204, 8 deaths vs 4 baseline = +100%)
    # beats the sup5 (lowest-GPM = 201, 18 deaths vs 10 baseline = +80%). That's the fairness win.
    assert target["account_id"] == 204
    assert role == "carry"


# ─────────────────────────────────────────────────────────────────────────────
# Prompt formatting includes role + factors

def test_format_blame_prompt_includes_role_and_factors():
    team = _five_player_team(
        radiant=True,
        kdas=[(2, 12, 4), (5, 5, 8), (4, 6, 9), (3, 8, 10), (1, 14, 12)],
        gpms=[700, 550, 400, 350, 280],
        account_ids=[101, 102, 103, 104, 105],
        hero_ids=[10, 11, 12, 13, 14],
    )
    match = {"match_id": 999, "duration": 1800, "radiant_win": False,
             "players": team + _five_player_team(False, [(0,0,0)]*5, [500]*5,
                                                 [201,202,203,204,205], [15,16,17,18,19])}
    heroes = {h: {"name": f"npc_dota_hero_{h}", "localized_name": f"Hero{h}"}
              for h in [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]}

    target, role, factors = coach._pick_blame_target(match, losing_team_radiant=True,
                                                      exclude_account=None)
    prompt = coach._format_blame_prompt(match, target, heroes,
                                         losing_team_radiant=True,
                                         role=role, factors=factors)
    # Role label appears
    assert "Position" in prompt
    # Blame factors section appears
    assert "blame factors" in prompt.lower() or "deviation" in prompt.lower()
    # Numbers from factors appear
    for f in factors.values():
        assert str(f["value"]) in prompt
