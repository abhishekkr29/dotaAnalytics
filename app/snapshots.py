import json

from tqdm import tqdm

from app import config, db, fetcher

_TOWER_TEAM_RADIANT = 2
_TOWER_TEAM_DIRE = 3


def _hero_compositions(match: dict) -> tuple[list[int], list[int]]:
    """Return (radiant_hero_ids[5], dire_hero_ids[5]) sorted within team for stability."""
    radiant: list[int] = []
    dire: list[int] = []
    for p in match.get("players") or []:
        hero_id = p.get("hero_id") or 0
        if (p.get("player_slot") or 0) < 128:
            radiant.append(hero_id)
        else:
            dire.append(hero_id)
    radiant = sorted(radiant)[:5] + [0] * max(0, 5 - len(radiant))
    dire = sorted(dire)[:5] + [0] * max(0, 5 - len(dire))
    return radiant[:5], dire[:5]


def extract(match: dict) -> list[dict]:
    if not fetcher.is_parsed(match):
        return []
    gold_adv = match.get("radiant_gold_adv") or []
    xp_adv = match.get("radiant_xp_adv") or []
    n = min(len(gold_adv), len(xp_adv))
    if n == 0:
        return []

    rt = fetcher.avg_rank_tier(match)
    radiant_win = bool(match["radiant_win"])

    tower_r = [0] * n
    tower_d = [0] * n
    rosh = [0] * n
    for o in match.get("objectives") or []:
        t = max(0, (o.get("time") or 0) // 60)
        if t >= n:
            continue
        otype = o.get("type", "")
        team = o.get("team")
        if otype == "CHAT_MESSAGE_TOWER_KILL":
            if team == _TOWER_TEAM_RADIANT:
                tower_d[t] += 1
            elif team == _TOWER_TEAM_DIRE:
                tower_r[t] += 1
        elif otype == "CHAT_MESSAGE_ROSHAN_KILL":
            rosh[t] += 1
    for i in range(1, n):
        tower_r[i] += tower_r[i - 1]
        tower_d[i] += tower_d[i - 1]
        rosh[i] += rosh[i - 1]

    kills_r = [0] * n
    kills_d = [0] * n
    for p in match.get("players") or []:
        is_radiant = (p.get("player_slot") or 0) < 128
        for k in p.get("kills_log") or []:
            t = max(0, (k.get("time") or 0) // 60)
            if t < n:
                if is_radiant:
                    kills_r[t] += 1
                else:
                    kills_d[t] += 1
    for i in range(1, n):
        kills_r[i] += kills_r[i - 1]
        kills_d[i] += kills_d[i - 1]

    r_heroes, d_heroes = _hero_compositions(match)

    return [
        {
            "match_id": match["match_id"],
            "minute": t,
            "gold_adv": int(gold_adv[t]),
            "xp_adv": int(xp_adv[t]),
            "tower_kills_radiant": tower_r[t],
            "tower_kills_dire": tower_d[t],
            "kills_radiant": kills_r[t],
            "kills_dire": kills_d[t],
            "roshan_kills": rosh[t],
            "avg_rank_tier": rt,
            "r_hero_1": r_heroes[0], "r_hero_2": r_heroes[1], "r_hero_3": r_heroes[2],
            "r_hero_4": r_heroes[3], "r_hero_5": r_heroes[4],
            "d_hero_1": d_heroes[0], "d_hero_2": d_heroes[1], "d_hero_3": d_heroes[2],
            "d_hero_4": d_heroes[3], "d_hero_5": d_heroes[4],
            "radiant_win": radiant_win,
        }
        for t in range(n)
    ]


_INSERT_SQL = """
INSERT INTO snapshots (
    match_id, minute, gold_adv, xp_adv,
    tower_kills_radiant, tower_kills_dire,
    kills_radiant, kills_dire, roshan_kills,
    avg_rank_tier,
    r_hero_1, r_hero_2, r_hero_3, r_hero_4, r_hero_5,
    d_hero_1, d_hero_2, d_hero_3, d_hero_4, d_hero_5,
    radiant_win
) VALUES (
    %(match_id)s, %(minute)s, %(gold_adv)s, %(xp_adv)s,
    %(tower_kills_radiant)s, %(tower_kills_dire)s,
    %(kills_radiant)s, %(kills_dire)s, %(roshan_kills)s,
    %(avg_rank_tier)s,
    %(r_hero_1)s, %(r_hero_2)s, %(r_hero_3)s, %(r_hero_4)s, %(r_hero_5)s,
    %(d_hero_1)s, %(d_hero_2)s, %(d_hero_3)s, %(d_hero_4)s, %(d_hero_5)s,
    %(radiant_win)s
) ON CONFLICT DO NOTHING
"""


def build_all(only_missing: bool = True) -> dict:
    with db.connect() as conn:
        if only_missing:
            sql = (
                "SELECT m.match_id FROM matches m "
                "LEFT JOIN snapshots s ON s.match_id = m.match_id "
                "WHERE m.parsed AND s.match_id IS NULL "
                "GROUP BY m.match_id"
            )
        else:
            sql = "SELECT match_id FROM matches WHERE parsed"
        match_ids = [r[0] for r in conn.execute(sql).fetchall()]

        n_matches = n_rows = 0
        for mid in tqdm(match_ids, desc="snapshots"):
            cache = config.MATCHES_DIR / f"{mid}.json"
            if not cache.exists():
                continue
            m = json.loads(cache.read_text())
            snaps = extract(m)
            if not snaps:
                continue
            with conn.cursor() as cur:
                cur.executemany(_INSERT_SQL, snaps)
            n_matches += 1
            n_rows += len(snaps)

    return {"matches_processed": n_matches, "snapshot_rows": n_rows}
