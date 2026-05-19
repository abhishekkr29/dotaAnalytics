"""Tests for the per-leak recommendation pipeline (pure-Python pieces only — no Anthropic calls)."""

from app import coach


def test_parse_clean_json():
    text = '{"1": "First rec.", "2": "Second rec."}'
    out = coach._parse_per_leak_response(text)
    assert out == {"1": "First rec.", "2": "Second rec."}


def test_parse_strips_markdown_fences():
    text = '```json\n{"1": "Trim fences."}\n```'
    out = coach._parse_per_leak_response(text)
    assert out == {"1": "Trim fences."}


def test_parse_handles_bare_fence():
    text = '```\n{"1": "No json tag."}\n```'
    out = coach._parse_per_leak_response(text)
    assert out == {"1": "No json tag."}


def test_parse_falls_back_on_numbered_lines():
    text = (
        '1. First rec text here.\n'
        '2. Second rec text here.\n'
        '3. Third rec.\n'
    )
    out = coach._parse_per_leak_response(text)
    assert "1" in out
    assert "First rec text here." in out["1"]
    assert "Third rec." in out["3"]


def test_format_prompt_includes_match_meta_and_leaks():
    report = {
        "match_id": 8804200251,
        "you": {"hero": "Drow Ranger", "team": "dire", "kda": "14/12/10",
                "result": "loss", "lane_role": "safe"},
        "duration_min": 32,
        "match": {"avg_rank_tier": 44},
    }
    leak_contexts = [
        "LEAK 1:\n  When: 14:04\n  Type: death\n  Detail: Died to QoP",
        "LEAK 2:\n  When: 06:30\n  Type: death\n  Detail: Died to Shaman",
    ]
    prompt = coach._format_per_leak_prompt(report, leak_contexts)
    assert "8804200251" in prompt
    assert "Drow Ranger" in prompt
    assert "Archon" in prompt   # bucket 4 maps to Archon
    assert "LEAK 1" in prompt
    assert "LEAK 2" in prompt
    assert "JSON only" in prompt


def test_build_leak_context_renders_compactly():
    """Synthetic match → leak context block contains all the right fields."""
    match = {
        "duration": 1800,
        "players": [
            # Your player (Storm Spirit, hero_id=74)
            {
                "player_slot": 0, "account_id": 446619601, "hero_id": 74,
                "kills": 5, "deaths": 2, "assists": 8,
                "gold_t": [600, 900, 1300, 1700, 2200, 2700, 3300, 3900, 4500, 5100, 5800],
                "xp_t":   [0, 300, 700, 1200, 1800, 2400, 3100, 3800, 4500, 5300, 6100],
                "lh_t":   [0, 12, 28, 45, 65, 80, 100, 120, 140, 160, 185],
                "purchase_log": [
                    {"key": "blink", "time": 720},
                    {"key": "black_king_bar", "time": 900},
                ],
                "kills_log": [],
                "obs_log": [], "sen_log": [], "buyback_log": [],
            },
            # Enemy carry (Pudge, hero_id=14)
            {
                "player_slot": 128, "account_id": 5, "hero_id": 14,
                "kills": 3, "deaths": 3, "assists": 4,
                "gold_t": [600, 850, 1200, 1700, 2200, 2700, 3300, 3900, 4400, 4900, 5500],
                "xp_t":   [0, 400, 900, 1500, 2200, 2900, 3700, 4500, 5300, 6100, 7000],
                "lh_t":   [0, 5, 18, 32, 50, 65, 80, 95, 110, 125, 140],
                "purchase_log": [
                    {"key": "force_staff", "time": 700},
                ],
                "kills_log": [
                    {"key": "npc_dota_hero_storm_spirit", "time": 510},  # killed you at 8:30
                ],
                "obs_log": [], "sen_log": [], "buyback_log": [],
            },
        ],
    }
    heroes = {
        74: {"name": "npc_dota_hero_storm_spirit", "localized_name": "Storm Spirit"},
        14: {"name": "npc_dota_hero_pudge",        "localized_name": "Pudge"},
    }
    # Leak is at 15:00 — both your purchases (Blink @12:00, BKB @15:00) should appear,
    # and Pudge's Force Staff @11:40 should also appear in his items
    leak = {"t": "15:00", "type": "death", "detail": "Died to Pudge", "impact": -0.05}
    ctx = coach._build_leak_context(
        leak_idx=1, leak=leak, match=match, heroes=heroes,
        account_id=446619601, your_radiant=True,
        curve=[0.50, 0.52, 0.55, 0.51, 0.48, 0.44, 0.42, 0.46, 0.45, 0.40],
    )
    assert "LEAK 1:" in ctx
    assert "15:00" in ctx
    assert "Died to Pudge" in ctx
    assert "Storm Spirit" in ctx
    assert "Pudge" in ctx
    assert "BKB" in ctx                 # your item bought at 15:00
    assert "Blink" in ctx               # your item bought at 12:00
    assert "Force Staff" in ctx         # enemy item bought at 11:40


def test_level_from_xp():
    """Sanity check the level lookup."""
    player = {"xp_t": [0, 230, 600, 1080, 1660]}
    # At minute 0 → 1 (just spawned)
    assert coach._level_at_time(player, 0) == 1
    # At minute 4 → ~L5 (>=1660)
    assert coach._level_at_time(player, 240) == 5


def test_gold_at_time():
    player = {"gold_t": [625, 900, 1400, 2100, 2800]}
    assert coach._gold_at_time(player, 0) == 625
    assert coach._gold_at_time(player, 180) == 2100   # minute 3
    # Out of range → last value (clamps)
    assert coach._gold_at_time(player, 99999) == 2800


def test_context_excludes_events_after_leak_time():
    """A smoke or kill that happens AFTER the leak timestamp must not appear in the context.
    Regression guard: a previous version included ±90s which let the LLM hallucinate
    causal stories like 'they smoked at t+47s' which is temporally impossible."""
    leak_at = 756   # 12:36
    match = {
        "duration": 1800,
        "players": [
            {
                "player_slot": 0, "account_id": 446619601, "hero_id": 74,
                "kills": 0, "deaths": 1, "assists": 0,
                "gold_t": [0]*15, "xp_t": [0]*15, "lh_t": [0]*15,
                "purchase_log": [], "kills_log": [],
                "obs_log": [], "sen_log": [], "buyback_log": [],
            },
            # Enemy: smokes BEFORE the leak (should appear) and AFTER (should NOT)
            {
                "player_slot": 128, "account_id": 5, "hero_id": 71,
                "kills": 3, "deaths": 0, "assists": 5,
                "gold_t": [0]*15, "xp_t": [0]*15, "lh_t": [0]*15,
                "purchase_log": [
                    {"key": "smoke_of_deceit", "time": 700},   # 11:40 — BEFORE leak — keep
                    {"key": "smoke_of_deceit", "time": 803},   # 13:23 — AFTER leak — drop
                ],
                "kills_log": [
                    {"key": "npc_dota_hero_storm_spirit", "time": leak_at},   # the death itself
                    {"key": "npc_dota_hero_pudge",        "time": 900},        # 15:00 — AFTER leak — drop
                ],
                "obs_log": [], "sen_log": [], "buyback_log": [],
            },
        ],
    }
    heroes = {
        74: {"name": "npc_dota_hero_storm_spirit", "localized_name": "Storm Spirit"},
        71: {"name": "npc_dota_hero_grimstroke",   "localized_name": "Grimstroke"},
        14: {"name": "npc_dota_hero_pudge",        "localized_name": "Pudge"},
    }
    leak = {"t": "12:36", "type": "death", "detail": "Died to Grimstroke", "impact": -0.099}
    ctx = coach._build_leak_context(
        leak_idx=1, leak=leak, match=match, heroes=heroes,
        account_id=446619601, your_radiant=True,
        curve=[0.50] * 20,
    )
    # The pre-leak smoke must appear; the post-leak smoke must NOT
    assert "11:40" in ctx, "pre-leak smoke at 11:40 should be present"
    assert "13:23" not in ctx, "post-leak smoke at 13:23 leaked into context (regression)"
    # Same for kills: the death-defining kill at 12:36 can appear; the 15:00 kill must not
    assert "15:00" not in ctx, "post-leak kill at 15:00 leaked into context (regression)"


def test_items_at_time_filters_by_key_items_and_time():
    player = {
        "purchase_log": [
            {"key": "tango", "time": 0},               # not in KEY_ITEMS
            {"key": "blink", "time": 720},             # in KEY_ITEMS, before t
            {"key": "black_king_bar", "time": 1080},   # in KEY_ITEMS, AFTER t — exclude
        ],
    }
    items = coach._items_at_time(player, 900)
    assert any("Blink" in i for i in items)
    assert not any("BKB" in i for i in items)
