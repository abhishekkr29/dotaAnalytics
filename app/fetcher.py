import json
import time

import requests
from tqdm import tqdm

from app import config, db


_RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 524}


def _get(path: str, params: dict | None = None) -> dict | list:
    url = f"{config.OPENDOTA_BASE}{path}"
    last_resp = None
    for attempt in range(6):
        try:
            last_resp = requests.get(url, params=params or {}, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if last_resp.status_code in _RETRYABLE_STATUSES:
            time.sleep(2 ** attempt)
            continue
        last_resp.raise_for_status()
        return last_resp.json()
    if last_resp is None:
        raise RuntimeError(f"network failure on {url}")
    last_resp.raise_for_status()


def _post(path: str) -> None:
    """POST with backoff on 429 and 5xx/CF errors — mirrors `_get`."""
    url = f"{config.OPENDOTA_BASE}{path}"
    last_resp = None
    for attempt in range(6):
        try:
            last_resp = requests.post(url, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if last_resp.status_code in _RETRYABLE_STATUSES:
            time.sleep(2 ** attempt)
            continue
        last_resp.raise_for_status()
        return
    if last_resp is None:
        raise RuntimeError(f"network failure on POST {url}")
    last_resp.raise_for_status()


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


def _explorer_parse_status(match_ids: list[int], chunk: int = 50) -> set[int]:
    """Return the subset of match_ids that are now parsed in OpenDota's matches table.

    Anything not returned is still unparsed. Batches into chunks of `chunk` IDs (smaller
    chunks are gentler on OpenDota's /explorer endpoint and less likely to 522). A failing
    chunk is logged and skipped — refresh-parses continues with the rest.
    """
    if not match_ids:
        return set()
    parsed: set[int] = set()
    n_chunks = (len(match_ids) + chunk - 1) // chunk
    for i in range(0, len(match_ids), chunk):
        chunk_idx = i // chunk + 1
        ids = ",".join(str(mid) for mid in match_ids[i:i + chunk])
        try:
            rows = _explorer(
                f"SELECT match_id FROM matches WHERE match_id IN ({ids}) AND version IS NOT NULL"
            )
            parsed.update(r["match_id"] for r in rows)
        except (requests.HTTPError, RuntimeError) as e:
            print(f"  explorer chunk {chunk_idx}/{n_chunks} failed ({e}); continuing")
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
    _post(f"/request/{match_id}")


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


def request_parses(limit: int = 200, cooldown_hours: int = 24) -> dict:
    """POST /request/<id> for unparsed matches, skipping those requested within `cooldown_hours`.

    Cooldown stops us from burning API quota on duplicate parse requests for matches
    OpenDota is already processing. Default 24h — OpenDota's parse queue can run hours
    behind during peak, and a still-unparsed match a few hours later is almost certainly
    queued (not dropped). After 24h we retry in case OpenDota genuinely lost the request.
    """
    with db.connect() as conn:
        total_unparsed = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE NOT parsed"
        ).fetchone()[0]
        eligible = [r[0] for r in conn.execute(
            """SELECT match_id FROM matches
               WHERE NOT parsed
                 AND (parse_requested_at IS NULL
                      OR parse_requested_at < NOW() - %s * INTERVAL '1 hour')
               ORDER BY start_time DESC
               LIMIT %s""",
            (cooldown_hours, limit),
        ).fetchall()]

    skipped_in_cooldown = total_unparsed - len(eligible)
    ok = failed = 0
    with db.connect() as conn:
        for mid in tqdm(eligible, desc="requesting parses"):
            try:
                request_parse(mid)
                conn.execute(
                    "UPDATE matches SET parse_requested_at = NOW() WHERE match_id = %s",
                    (mid,),
                )
                ok += 1
            except requests.HTTPError as e:
                tqdm.write(f"skip {mid}: {e}")
                failed += 1
    return {
        "unparsed_in_db": total_unparsed,
        "in_cooldown": skipped_in_cooldown,
        "eligible": len(eligible),
        "requested": ok,
        "failed": failed,
    }


def refresh_parses(limit: int = 200) -> dict:
    """Check parse status of unparsed matches by fetching /matches/{id} per match.

    The earlier /explorer-based batched check turned out to be unreliable — OpenDota's
    `matches` SQL table on /explorer lags behind the actual parse state visible via
    /matches/{id}, sometimes by hours. So we fall back to per-match checks here.

    Ordering: oldest-requested-first (NULLS FIRST so never-requested are checked first too).
    Caller controls per-cycle cost via `limit`. Typical: 200 → ~200 API calls per cycle,
    fits under free-tier quota with room for bracket-fetch / request-parses.
    """
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT match_id FROM matches "
            "WHERE NOT parsed "
            "ORDER BY parse_requested_at ASC NULLS FIRST "
            "LIMIT %s",
            (limit,),
        ).fetchall()]

    if not ids:
        return {"unparsed_in_db": 0, "checked": 0, "newly_parsed": 0, "api_calls": 0}

    api_calls = 0
    newly_parsed = 0
    with db.connect() as conn:
        for mid in tqdm(ids, desc="checking parse status"):
            try:
                m = fetch_match(mid, force=True)
                api_calls += 1
                upsert_match(conn, m)
                if is_parsed(m):
                    newly_parsed += 1
            except requests.HTTPError as e:
                tqdm.write(f"skip {mid}: {e}")

    return {
        "unparsed_in_db": len(ids),
        "checked": api_calls,
        "newly_parsed": newly_parsed,
        "api_calls": api_calls,
    }
