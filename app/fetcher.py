import json
import re
import time

import requests
from tqdm import tqdm

from app import config, db


_RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 524}

_API_KEY_RE = re.compile(r'([?&])api_key=[^&\s]*')


def _safe_error(exc: Exception) -> str:
    """Stringify an exception with any `api_key=…` query param masked.

    `requests.HTTPError.__str__()` includes the failing URL with all query params.
    Without masking, our Premium API key would leak into error logs and stdout
    on every retryable failure (429, 5xx, 522). This helper strips it out.
    """
    return _API_KEY_RE.sub(r'\1api_key=***', str(exc))


def _params_with_api_key(params: dict | None) -> dict:
    """Inject the OpenDota Premium API key into request params if set."""
    out = dict(params or {})
    if config.OPENDOTA_API_KEY:
        out.setdefault("api_key", config.OPENDOTA_API_KEY)
    return out


def _get(path: str, params: dict | None = None) -> dict | list:
    url = f"{config.OPENDOTA_BASE}{path}"
    merged = _params_with_api_key(params)
    last_resp = None
    for attempt in range(6):
        try:
            last_resp = requests.get(url, params=merged, timeout=60)
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
    merged = _params_with_api_key(None)
    last_resp = None
    for attempt in range(6):
        try:
            last_resp = requests.post(url, params=merged, timeout=30)
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


def _explorer_parse_status(match_ids: list[int], chunk: int = 50) -> set[int]:
    """Return the subset of `match_ids` that OpenDota's `matches` table reports as parsed.

    Batched at `chunk` IDs per /explorer call (smaller is gentler on Cloudflare).
    Chunk failures are logged and skipped — the caller treats them as "not parsed yet"
    and we pick them up on the next cycle.

    Note: OpenDota's `matches` SQL table lags real-time parse status by up to a few
    hours. Used in `refresh_parses(mode="explorer")` for cheap bulk status checks;
    hybrid mode adds a per-match fallback for matches that stay stuck in the lag window.
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
            print(f"  explorer chunk {chunk_idx}/{n_chunks} failed ({_safe_error(e)}); continuing")
    return parsed


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


def fetch_profile(account_id: int, force: bool = False) -> dict:
    path = config.profile_path(account_id)
    if path.exists() and not force:
        return json.loads(path.read_text())
    data = _get(f"/players/{account_id}")
    path.write_text(json.dumps(data, indent=2))
    return data


def rank_tier_for(account_id: int) -> int:
    p = fetch_profile(account_id)
    rt = p.get("rank_tier")
    if rt is None:
        raise SystemExit(f"OpenDota has no rank_tier for account {account_id} yet")
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


def player_for(match: dict, account_id: int) -> dict:
    for p in match.get("players", []):
        if p.get("account_id") == account_id:
            return p
    return {}


def _upsert_user_match(conn, account_id: int, match_id: int, player: dict) -> None:
    """Record that `account_id` was in this match. Creates a stub users row if needed."""
    conn.execute(
        "INSERT INTO users (account_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (account_id,),
    )
    conn.execute(
        """
        INSERT INTO user_matches (user_id, match_id, slot, hero_id, kills, deaths, assists)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, match_id) DO UPDATE SET
            slot    = EXCLUDED.slot,
            hero_id = EXCLUDED.hero_id,
            kills   = EXCLUDED.kills,
            deaths  = EXCLUDED.deaths,
            assists = EXCLUDED.assists;
        """,
        (
            account_id, match_id,
            player.get("player_slot"), player.get("hero_id"),
            player.get("kills"), player.get("deaths"), player.get("assists"),
        ),
    )


def upsert_match(conn, match: dict, account_id: int | None = None) -> None:
    """Upsert a full match row. If `account_id` is given and that user is in players[],
    also record the per-user link in user_matches."""
    conn.execute(
        """
        INSERT INTO matches (
            match_id, start_time, duration, game_mode, lobby_type,
            radiant_win, avg_rank_tier, parsed, patch
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (match_id) DO UPDATE SET
            parsed        = EXCLUDED.parsed,
            avg_rank_tier = EXCLUDED.avg_rank_tier,
            patch         = COALESCE(EXCLUDED.patch, matches.patch),
            fetched_at    = NOW();
        """,
        (
            match["match_id"], match["start_time"], match["duration"],
            match["game_mode"], match["lobby_type"], match["radiant_win"],
            avg_rank_tier(match), is_parsed(match), match.get("patch"),
        ),
    )
    if account_id:
        you = player_for(match, account_id)
        if you:
            _upsert_user_match(conn, account_id, match["match_id"], you)


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


def sync_match(match_id: int, account_id: int | None = None) -> dict:
    with db.connect() as conn:
        m = fetch_match(match_id)
        upsert_match(conn, m, account_id=account_id)
    return {
        "match_id": match_id,
        "parsed": is_parsed(m),
        "duration": m.get("duration"),
        "avg_rank_tier": avg_rank_tier(m),
        "radiant_win": m.get("radiant_win"),
    }


def sync_bracket_matches(
    account_id: int,
    limit: int = 500,
    window: int = 10,
    rank_tier: int | None = None,
) -> dict:
    """3N→1N: one /explorer call discovers IDs + parse status. JSONs fetched only when parsed.

    `rank_tier`: target bracket. Default `None` → use the signed-in account's own rank.
    Passing an explicit rank lets us bootstrap training data across every bracket
    (Path C in PLANNING.md) without changing whose account drives the fetch.
    """
    rt = rank_tier if rank_tier is not None else rank_tier_for(account_id)
    rows = _explorer_bracket(rt, window=window, limit=limit)

    discovered = len(rows)
    parsed_in_discovery = sum(1 for r in rows if r["parsed"])

    with db.connect() as conn:
        for r in tqdm(rows, desc=f"upsert {rt}±{window}"):
            upsert_summary(conn, r)

        to_fetch = [
            r["match_id"] for r in rows
            if r["parsed"] and not (config.MATCHES_DIR / f"{r['match_id']}.json").exists()
        ]
        fetched_json = 0
        for mid in tqdm(to_fetch, desc="fetch parsed JSONs"):
            try:
                m = fetch_match(mid)
                upsert_match(conn, m, account_id=account_id)
                fetched_json += 1
            except requests.HTTPError as e:
                tqdm.write(f"skip {mid}: {_safe_error(e)}")

    return {
        "rank_tier": rt, "window": window,
        "discovered": discovered,
        "parsed_at_discovery": parsed_in_discovery,
        "json_fetched": fetched_json,
        "api_calls": 1 + len(to_fetch),
    }


def request_parses(
    limit: int = 200,
    cooldown_hours: int = 24,
    rank_min: int | None = None,
    rank_max: int | None = None,
) -> dict:
    """POST /request/<id> for unparsed matches, skipping those requested within `cooldown_hours`.

    `rank_min` / `rank_max` (inclusive) narrow the queue to a rank slice — used by
    `scripts/collect_data.sh` to prioritize sparse high-rank brackets that would
    otherwise starve behind a flood of Herald/Guardian requests.
    """
    base_where = ["NOT parsed"]
    base_params: list = []
    if rank_min is not None:
        base_where.append("avg_rank_tier >= %s")
        base_params.append(rank_min)
    if rank_max is not None:
        base_where.append("avg_rank_tier <= %s")
        base_params.append(rank_max)
    base_clause = " AND ".join(base_where)

    with db.connect() as conn:
        total_unparsed = conn.execute(
            f"SELECT COUNT(*) FROM matches WHERE {base_clause}",
            base_params,
        ).fetchone()[0]
        eligible = [r[0] for r in conn.execute(
            f"""SELECT match_id FROM matches
                WHERE {base_clause}
                  AND (parse_requested_at IS NULL
                       OR parse_requested_at < NOW() - %s * INTERVAL '1 hour')
                ORDER BY start_time DESC
                LIMIT %s""",
            base_params + [cooldown_hours, limit],
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
                tqdm.write(f"skip {mid}: {_safe_error(e)}")
                failed += 1
    out = {
        "unparsed_in_db": total_unparsed,
        "in_cooldown": skipped_in_cooldown,
        "eligible": len(eligible),
        "requested": ok,
        "failed": failed,
    }
    if rank_min is not None or rank_max is not None:
        out["rank_filter"] = {"min": rank_min, "max": rank_max}
    return out


def refresh_parses(
    limit: int = 200,
    account_id: int | None = None,
    retrain_hint_threshold: int = 500,
    mode: str = "explorer",
    fallback_after_hours: int = 48,
) -> dict:
    """Check parse status of unparsed matches.

    `mode`:
      - "per_match" (reliable; recommended): GET /matches/{id} for every unparsed
        row. Real-time and accurate. Costs N API calls per N matches.
      - "explorer" (cheap when reliable): one /explorer batch (~50 IDs each)
        returns the subset that is now parsed in OpenDota's matches table.
        ~50× fewer API calls. **Caveat:** OpenDota's `matches` SQL table
        observed lagging by 6+ hours on 2026-05-18 — it can miss matches that
        are actually parsed. Use this only when the freshness lag is acceptable
        (e.g., periodic background sync, not time-critical workflows).
      - "hybrid": explorer-batch for the bulk, then per-match for any unparsed
        match where `parse_requested_at < NOW() - fallback_after_hours h`.

    Emits a retrain hint when more than `retrain_hint_threshold` parsed matches
    have accumulated since the last train run.
    """
    if mode not in ("explorer", "per_match", "hybrid"):
        raise ValueError(f"unknown mode: {mode!r}")

    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT match_id FROM matches "
            "WHERE NOT parsed "
            "ORDER BY parse_requested_at ASC NULLS FIRST "
            "LIMIT %s",
            (limit,),
        ).fetchall()]

    if not ids:
        return {"unparsed_in_db": 0, "checked": 0, "newly_parsed": 0, "api_calls": 0, "mode": mode}

    api_calls = 0
    newly_parsed = 0

    if mode == "per_match":
        # Reliable path: fetch every match's full JSON. Used by collect_data.sh
        # because /explorer's lag has burned us in production.
        with db.connect() as conn:
            for mid in tqdm(ids, desc="per-match check"):
                try:
                    m = fetch_match(mid, force=True)
                    api_calls += 1
                    upsert_match(conn, m, account_id=account_id)
                    if is_parsed(m):
                        newly_parsed += 1
                except requests.HTTPError as e:
                    tqdm.write(f"skip {mid}: {_safe_error(e)}")
    else:
        # 1. Cheap batch status check
        parsed_set = _explorer_parse_status(ids)
        # /explorer chunks at 50 IDs internally; api_calls ≈ ceil(len(ids)/50)
        api_calls += (len(ids) + 49) // 50

        # 2. For confirmed-parsed: fetch JSON + upsert
        with db.connect() as conn:
            for mid in tqdm(parsed_set, desc="fetching newly-parsed"):
                try:
                    m = fetch_match(mid, force=True)
                    api_calls += 1
                    upsert_match(conn, m, account_id=account_id)
                    if is_parsed(m):
                        newly_parsed += 1
                except requests.HTTPError as e:
                    tqdm.write(f"skip {mid}: {_safe_error(e)}")

        # 3. Hybrid: per-match fallback for matches pending too long without /explorer ack
        if mode == "hybrid":
            with db.connect() as conn:
                stale = [r[0] for r in conn.execute(
                    """SELECT match_id FROM matches
                       WHERE NOT parsed
                         AND match_id != ALL(%s)
                         AND parse_requested_at IS NOT NULL
                         AND parse_requested_at < NOW() - %s * INTERVAL '1 hour'
                       LIMIT 50""",
                    (list(parsed_set), fallback_after_hours),
                ).fetchall()]
            if stale:
                with db.connect() as conn:
                    for mid in tqdm(stale, desc=f"per-match fallback (>{fallback_after_hours}h)"):
                        try:
                            m = fetch_match(mid, force=True)
                            api_calls += 1
                            upsert_match(conn, m, account_id=account_id)
                            if is_parsed(m):
                                newly_parsed += 1
                        except requests.HTTPError as e:
                            tqdm.write(f"skip {mid}: {_safe_error(e)}")

    result = {
        "mode": mode,
        "unparsed_in_db": len(ids),
        "checked": len(ids),
        "newly_parsed": newly_parsed,
        "api_calls": api_calls,
    }

    try:
        from app import train as train_mod
        status = train_mod.parsed_count_since_train()
        if status.get("delta") is not None and status["delta"] >= retrain_hint_threshold:
            result["retrain_hint"] = (
                f"{status['delta']} new parsed matches since last train "
                f"(threshold: {retrain_hint_threshold}). Consider running `train`."
            )
    except Exception:
        pass

    return result


