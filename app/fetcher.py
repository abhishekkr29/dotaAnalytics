import json
import time

import psycopg
import requests
from tqdm import tqdm

from app import config


def _get(path: str, params: dict | None = None) -> dict | list:
    url = f"{config.OPENDOTA_BASE}{path}"
    for attempt in range(6):
        r = requests.get(url, params=params or {}, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"giving up on {url}")


def list_player_turbo_matches(limit: int = 200) -> list[dict]:
    return _get(
        f"/players/{config.require_account_id()}/matches",
        {"game_mode": config.TURBO_GAME_MODE, "limit": limit},
    )


def fetch_match(match_id: int, force: bool = False) -> dict:
    cache = config.MATCHES_DIR / f"{match_id}.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text())
    data = _get(f"/matches/{match_id}")
    cache.write_text(json.dumps(data))
    return data


def request_parse(match_id: int) -> None:
    requests.post(f"{config.OPENDOTA_BASE}/request/{match_id}", timeout=30).raise_for_status()


def is_parsed(match: dict) -> bool:
    if not match.get("version"):
        return False
    players = match.get("players") or []
    return bool(players and players[0].get("gold_t"))


def _avg_rank_tier(match: dict) -> int | None:
    tiers = [p["rank_tier"] for p in match.get("players", []) if p.get("rank_tier")]
    return sum(tiers) // len(tiers) if tiers else None


def _your_player(match: dict) -> dict:
    aid = config.require_account_id()
    for p in match.get("players", []):
        if p.get("account_id") == aid:
            return p
    return {}


def upsert_match(conn: psycopg.Connection, match: dict) -> None:
    you = _your_player(match)
    conn.execute(
        """
        INSERT INTO matches (
            match_id, start_time, duration, game_mode, lobby_type,
            radiant_win, avg_rank_tier, parsed, patch,
            your_slot, your_hero_id, your_kills, your_deaths, your_assists
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (match_id) DO UPDATE SET
            parsed        = EXCLUDED.parsed,
            avg_rank_tier = EXCLUDED.avg_rank_tier,
            fetched_at    = NOW();
        """,
        (
            match["match_id"], match["start_time"], match["duration"],
            match["game_mode"], match["lobby_type"], match["radiant_win"],
            _avg_rank_tier(match), is_parsed(match), match.get("patch"),
            you.get("player_slot"), you.get("hero_id"),
            you.get("kills"), you.get("deaths"), you.get("assists"),
        ),
    )


def sync_player_matches(limit: int = 200) -> dict:
    summaries = list_player_turbo_matches(limit=limit)
    fetched = parsed = 0
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        for s in tqdm(summaries, desc="fetching"):
            mid = s["match_id"]
            try:
                m = fetch_match(mid)
            except requests.HTTPError as e:
                tqdm.write(f"skip {mid}: {e}")
                continue
            upsert_match(conn, m)
            fetched += 1
            if is_parsed(m):
                parsed += 1
    return {"history_size": len(summaries), "fetched": fetched, "parsed": parsed}
