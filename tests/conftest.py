"""Test setup — runs before any test module imports `app.*`.

Sets safe defaults so module-level env reads (`app.config`) don't fail in CI:
- FERNET_KEY / JWT_SECRET to deterministic test values
- DATA_DIR to a temporary directory unique to this test run
- ACCOUNT_ID unset so `require_account_id()` checks can be exercised
"""

import os
import shutil
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="dotaAnalytics-test-"))

os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("DATA_DIR", str(_TEST_DATA_DIR))
os.environ.pop("ACCOUNT_ID", None)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


# Synthetic match fixture used across tests — minimal valid OpenDota-shape match.
SYNTHETIC_MATCH = {
    "match_id": 12345,
    "game_mode": 23,
    "lobby_type": 7,
    "duration": 1200,
    "start_time": 1700000000,
    "radiant_win": True,
    "version": 1,
    "patch": 56,
    "avg_rank_tier": 43,
    "radiant_gold_adv": [0, 100, 250, 400, 800, 1200, 1500, 1800, 2100, 2500, 3000],
    "radiant_xp_adv":   [0, 80, 200, 380, 700, 1100, 1400, 1700, 2000, 2400, 2900],
    "objectives": [
        {"time": 240,  "type": "CHAT_MESSAGE_TOWER_KILL",  "team": 2},
        {"time": 600,  "type": "CHAT_MESSAGE_ROSHAN_KILL", "team": 2},
    ],
    "players": [
        # Radiant side (slots 0-4)
        {
            "player_slot": 0, "account_id": 446619601, "hero_id": 74,
            "kills": 5, "deaths": 2, "assists": 8,
            "gold_per_min": 520, "xp_per_min": 600, "net_worth": 14200,
            "lane_role": 2,
            "rank_tier": 43,
            "gold_t": [600, 800, 1200, 1600, 2100, 2700, 3200, 3800, 4400, 5000, 5800],
            "xp_t":   [0, 200, 500, 900, 1400, 2000, 2600, 3300, 4000, 4800, 5700],
            "lh_t":   [0, 10, 25, 42, 60, 78, 95, 110, 130, 148, 168],
            "purchase_log": [
                {"key": "blink",           "time": 720},
                {"key": "black_king_bar",  "time": 900},
            ],
            "kills_log": [{"key": "npc_dota_hero_pudge", "time": 480}],
            "obs_log": [],
            "sen_log": [],
            "buyback_log": [],
        },
        {"player_slot": 1, "account_id": 1, "hero_id": 10, "kills": 1, "deaths": 1, "assists": 5,
         "rank_tier": 41, "gold_per_min": 350, "xp_per_min": 400, "net_worth": 8000,
         "purchase_log": [], "kills_log": [], "obs_log": [], "sen_log": [], "buyback_log": []},
        {"player_slot": 2, "account_id": 2, "hero_id": 5,  "kills": 2, "deaths": 1, "assists": 3,
         "rank_tier": 44, "gold_per_min": 300, "xp_per_min": 380, "net_worth": 7500,
         "purchase_log": [], "kills_log": [], "obs_log": [], "sen_log": [], "buyback_log": []},
        {"player_slot": 3, "account_id": 3, "hero_id": 7,  "kills": 0, "deaths": 2, "assists": 4,
         "rank_tier": 42, "gold_per_min": 280, "xp_per_min": 360, "net_worth": 7000,
         "purchase_log": [], "kills_log": [], "obs_log": [], "sen_log": [], "buyback_log": []},
        {"player_slot": 4, "account_id": 4, "hero_id": 8,  "kills": 1, "deaths": 0, "assists": 6,
         "rank_tier": 45, "gold_per_min": 320, "xp_per_min": 410, "net_worth": 7800,
         "purchase_log": [], "kills_log": [], "obs_log": [], "sen_log": [], "buyback_log": []},
        # Dire side (slots 128-132); one of them is Pudge who kills you twice in a fight.
        {"player_slot": 128, "account_id": 5, "hero_id": 14, "kills": 3, "deaths": 3, "assists": 4,
         "rank_tier": 43, "gold_per_min": 410, "xp_per_min": 470, "net_worth": 10000,
         "purchase_log": [], "kills_log": [
            {"key": "npc_dota_hero_storm_spirit", "time": 510},  # ⇒ death @08:30
            {"key": "npc_dota_hero_storm_spirit", "time": 520},  # ⇒ death @08:40 (same fight)
         ], "obs_log": [], "sen_log": [], "buyback_log": []},
        {"player_slot": 129, "account_id": 6, "hero_id": 16, "kills": 1, "deaths": 3, "assists": 5,
         "rank_tier": 41, "gold_per_min": 320, "xp_per_min": 380, "net_worth": 8000,
         "purchase_log": [], "kills_log": [], "obs_log": [], "sen_log": [], "buyback_log": []},
        {"player_slot": 130, "account_id": 7, "hero_id": 20, "kills": 0, "deaths": 4, "assists": 4,
         "rank_tier": 40, "gold_per_min": 280, "xp_per_min": 350, "net_worth": 7000,
         "purchase_log": [], "kills_log": [], "obs_log": [], "sen_log": [], "buyback_log": []},
        {"player_slot": 131, "account_id": 8, "hero_id": 26, "kills": 1, "deaths": 2, "assists": 3,
         "rank_tier": 44, "gold_per_min": 290, "xp_per_min": 360, "net_worth": 7200,
         "purchase_log": [], "kills_log": [], "obs_log": [], "sen_log": [], "buyback_log": []},
        {"player_slot": 132, "account_id": 9, "hero_id": 30, "kills": 1, "deaths": 1, "assists": 4,
         "rank_tier": 42, "gold_per_min": 270, "xp_per_min": 340, "net_worth": 7100,
         "purchase_log": [], "kills_log": [], "obs_log": [], "sen_log": [], "buyback_log": []},
    ],
}


def synthetic_match() -> dict:
    """Return a deep copy so tests can mutate freely."""
    import copy
    return copy.deepcopy(SYNTHETIC_MATCH)
