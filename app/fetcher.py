import json
import time

import requests
from tqdm import tqdm

from app import config, db


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


def _explorer(sql: str) -> list[dict]:
    payload = _get("/explorer", {"sql": sql})
    if isinstance(payload, dict) and payload.get("err"):
        raise RuntimeError(f"explorer error: {payload['err']}")
    return (payload or {}).get("rows", []) if isinstance(payload, dict) else []


def _explorer_bracket(rank_tier: int, window: int = 10, limit: int = 5000) -> list[dict]:
    """LEFT JOIN public_matches × matches for bracket discovery + parse status in one call."""
    low, high = rank_tier - window, rank_tier + window
    sql = (
        "SELECT pm.match_id, pm.start_time, pm.duration, pm.lobby_type, "
        "       pm.radiant_win, pm.avg_rank_tier, "
        "       (m.version IS NOT NULL) AS parsed "
        "FROM public_matches pm "
        "LEFT JOIN matches m USING (match_id) "
        f"WHERE pm.game_mode = {config.TURBO_GAME_MODE} "
        "  AND pm.lobby_type IN (0, 7) "
        "  AND pm.duration > 480 "
        f"  AND pm.avg_rank_tier BETWEEN {low} AND {high} "
        "ORDER BY pm.start_time DESC "
        f"LIMIT {limit}"
    )
    return _explorer(sql)


def _explorer_parse_status(match_ids: list[int], chunk: int = 200) -> set[int]:
    """Return the subset of match_ids that are now parsed in OpenDota's matches table.

    Anything not returned is still unparsed. Batches into chunks of `chunk` IDs per query.
    """
    if not match_ids:
        return set()
    parsed: set[int] = set()
    for i in range(0, len(match_ids), chunk):
        ids = ",".join(str(mid) for mid in match_ids[i:i + chunk])
        rows = _explorer(
            f"SELECT match_id FROM matches WHERE match_id IN ({ids}) AND version IS NOT NULL"
        )
        parsed.update(r["match_id"] for r in rows)
    return parsed


def fetch_profile(force: bool = False) -> dict:
    if config.PROFILE_PATH.exists() and not force:
        return json.loads(config.PROFILE_PATH.read_text())
    data = _get(f"/players/{config.require_account_id()}")
    config.PROFILE_PATH.write_text(json.dumps(data, indent=2))
    return data


def your_rank_tier() -> int:
    p = fetch_profile()
    rt = p.get("rank_tier")
    if rt is None:
        raise SystemExit("OpenDota has no rank_tier for this account yet")
    return rt


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


def avg_rank_tier(match: dict) -> int | None:
    tiers = [p["rank_tier"] for p in match.get("players", []) if p.get("rank_tier")]
    return sum(tiers) // len(tiers) if tiers else None


def your_player(match: dict) -> dict:
    aid = config.require_account_id()
    for p in match.get("players", []):
        if p.get("account_id") == aid:
            return p
    return {}


def upsert_match(conn, match: dict) -> None:
    you = your_player(match)
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
            your_slot     = COALESCE(EXCLUDED.your_slot,     matches.your_slot),
            your_hero_id  = COALESCE(EXCLUDED.your_hero_id,  matches.your_hero_id),
            your_kills    = COALESCE(EXCLUDED.your_kills,    matches.your_kills),
            your_deaths   = COALESCE(EXCLUDED.your_deaths,   matches.your_deaths),
            your_assists  = COALESCE(EXCLUDED.your_assists,  matches.your_assists),
            patch         = COALESCE(EXCLUDED.patch,         matches.patch),
            fetched_at    = NOW();
        """,
        (
            match["match_id"], match["start_time"], match["duration"],
            match["game_mode"], match["lobby_type"], match["radiant_win"],
            avg_rank_tier(match), is_parsed(match), match.get("patch"),
            you.get("player_slot"), you.get("hero_id"),
            you.get("kills"), you.get("deaths"), you.get("assists"),
        ),
    )


def upsert_summary(conn, row: dict) -> None:
    """Upsert from a /explorer summary row (no full match JSON yet)."""
    conn.execute(
        """
        INSERT INTO matches (
            match_id, start_time, duration, game_mode, lobby_type,
            radiant_win, avg_rank_tier, parsed
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (match_id) DO UPDATE SET
            parsed        = EXCLUDED.parsed,
            avg_rank_tier = EXCLUDED.avg_rank_tier,
            fetched_at    = NOW();
        """,
        (
            row["match_id"], row["start_time"], row["duration"],
            config.TURBO_GAME_MODE, row["lobby_type"], row["radiant_win"],
            row["avg_rank_tier"], bool(row["parsed"]),
        ),
    )


def sync_match(match_id: int) -> dict:
    with db.connect() as conn:
        m = fetch_match(match_id)
        upsert_match(conn, m)
    return {
        "match_id": match_id,
        "parsed": is_parsed(m),
        "duration": m.get("duration"),
        "avg_rank_tier": avg_rank_tier(m),
        "radiant_win": m.get("radiant_win"),
    }


def sync_bracket_matches(limit: int = 500, window: int = 10) -> dict:
    """3N→1N: one /explorer call discovers IDs + parse status. JSONs fetched only when parsed."""
    rt = your_rank_tier()
    rows = _explorer_bracket(rt, window=window, limit=limit)

    discovered = len(rows)
    parsed_in_discovery = sum(1 for r in rows if r["parsed"])

    with db.connect() as conn:
        for r in tqdm(rows, desc=f"upsert {rt}±{window}"):
            upsert_summary(conn, r)

        # Fetch JSONs for parsed matches that we don't have on disk yet
        to_fetch = [
            r["match_id"] for r in rows
            if r["parsed"] and not (config.MATCHES_DIR / f"{r['match_id']}.json").exists()
        ]
        fetched_json = 0
        for mid in tqdm(to_fetch, desc="fetch parsed JSONs"):
            try:
                m = fetch_match(mid)
                upsert_match(conn, m)
                fetched_json += 1
            except requests.HTTPError as e:
                tqdm.write(f"skip {mid}: {e}")

    return {
        "rank_tier": rt, "window": window,
        "discovered": discovered,
        "parsed_at_discovery": parsed_in_discovery,
        "json_fetched": fetched_json,
        "api_calls": 1 + len(to_fetch),  # 1 explorer + N parsed JSON fetches
    }


def request_parses(limit: int = 200) -> dict:
    """POST /request/<id> for unparsed matches currently in the DB. 1 call per match."""
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT match_id FROM matches WHERE NOT parsed ORDER BY start_time DESC LIMIT %s",
            (limit,),
        ).fetchall()]
    ok = failed = 0
    for mid in tqdm(ids, desc="requesting parses"):
        try:
            request_parse(mid)
            ok += 1
        except requests.HTTPError as e:
            tqdm.write(f"skip {mid}: {e}")
            failed += 1
    return {"unparsed_in_db": len(ids), "requested": ok, "failed": failed}


def refresh_parses(limit: int = 500) -> dict:
    """Batched: 1 /explorer call per ~200 IDs to detect newly-parsed, then fetch their JSONs."""
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT match_id FROM matches WHERE NOT parsed ORDER BY start_time DESC LIMIT %s",
            (limit,),
        ).fetchall()]

    if not ids:
        return {"unparsed_in_db": 0, "newly_parsed": 0, "api_calls": 0}

    newly_parsed_ids = _explorer_parse_status(ids)
    explorer_calls = (len(ids) + 199) // 200

    fetched = 0
    with db.connect() as conn:
        for mid in tqdm(sorted(newly_parsed_ids), desc="fetching newly parsed"):
            try:
                m = fetch_match(mid, force=True)
                upsert_match(conn, m)
                fetched += 1
            except requests.HTTPError as e:
                tqdm.write(f"skip {mid}: {e}")

    return {
        "unparsed_in_db": len(ids),
        "newly_parsed": fetched,
        "api_calls": explorer_calls + fetched,
    }
