import json

import numpy as np
import requests
import xgboost as xgb

from app import config, fetcher, snapshots
from app.train import FEATURE_COLS

KEY_ITEMS = {
    "black_king_bar": "BKB",
    "ultimate_scepter": "Aghanim's Scepter",
    "pipe": "Pipe of Insight",
    "manta": "Manta Style",
    "assault": "Assault Cuirass",
    "abyssal_blade": "Abyssal Blade",
    "skadi": "Eye of Skadi",
    "refresher": "Refresher Orb",
    "octarine_core": "Octarine Core",
    "shivas_guard": "Shiva's Guard",
    "heart": "Heart of Tarrasque",
    "rapier": "Divine Rapier",
    "blink": "Blink Dagger",
    "force_staff": "Force Staff",
    "glimmer_cape": "Glimmer Cape",
    "cyclone": "Eul's Scepter",
    "blade_mail": "Blade Mail",
    "lotus_orb": "Lotus Orb",
    "sheepstick": "Scythe of Vyse",
    "satanic": "Satanic",
    "butterfly": "Butterfly",
    "crimson_guard": "Crimson Guard",
    "guardian_greaves": "Guardian Greaves",
    "vladmir": "Vladmir's Offering",
    "spirit_vessel": "Spirit Vessel",
    "ghost": "Ghost Scepter",
}

_TOWER_TEAM_RADIANT = 2

_LANE_ROLE = {1: "safe", 2: "mid", 3: "off", 4: "jungle"}


def heroes_by_id() -> dict[int, dict]:
    """Return {hero_id: hero_dict} from cached /heroes endpoint."""
    path = config.DATA_DIR / "heroes.json"
    if not path.exists():
        path.write_text(json.dumps(fetcher._get("/heroes")))
    return {h["id"]: h for h in json.loads(path.read_text())}


def _heroes_by_npc(heroes: dict[int, dict]) -> dict[str, dict]:
    return {h["name"]: h for h in heroes.values()}


def _load_model() -> tuple[xgb.XGBClassifier, list[str], dict]:
    """Load model + the matching feature subset + optional per-bracket calibrators.

    Backward-compat: if the model was trained with fewer features than the current
    FEATURE_COLS list, we trim to `n_features_in_` so adding new features later
    doesn't break inference on an older model. Calibrators are optional — when
    `data/calibrators.joblib` is missing we use raw model probabilities.
    """
    if not config.MODEL_PATH.exists():
        raise SystemExit(
            f"no model at {config.MODEL_PATH} — run `train` first "
            "(needs parsed snapshots in the DB)"
        )
    model = xgb.XGBClassifier()
    model.load_model(str(config.MODEL_PATH))
    n_features = int(getattr(model, "n_features_in_", len(FEATURE_COLS)))
    features = FEATURE_COLS[:n_features]

    cal_path = config.DATA_DIR / "calibrators.joblib"
    calibrators: dict = {}
    if cal_path.exists():
        try:
            import joblib
            calibrators = joblib.load(cal_path)
        except Exception as e:
            print(f"warning: failed to load calibrators ({e}); using raw probabilities")

    return model, features, calibrators


def _win_prob_curve(
    model: xgb.XGBClassifier,
    snaps: list[dict],
    features: list[str],
    calibrators: dict,
    match_rank_tier: int | None,
) -> list[float]:
    X = np.array([[s[c] for c in features] for s in snaps], dtype=np.float32)
    raw = model.predict_proba(X)[:, 1]
    bucket = (match_rank_tier // 10) if match_rank_tier else None
    if bucket is not None and bucket in calibrators:
        try:
            raw = calibrators[bucket].transform(raw)
        except Exception as e:
            print(f"warning: bracket {bucket} calibrator failed ({e}); falling back to raw")
    return [float(p) for p in raw]


def _format_time(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    s = abs(int(seconds))
    return f"{sign}{s // 60:02d}:{s % 60:02d}"


def _replay_url(match_id: int, t_seconds: int) -> str:
    """`dota2://` deep link that opens the replay at the given timestamp."""
    return f"dota2://matchid={match_id}&matchtime={max(0, t_seconds)}"


def _extract_decisions(
    match: dict,
    you: dict,
    heroes_by_id: dict[int, dict],
    heroes_by_npc: dict[str, dict],
    user_team_is_radiant: bool,
) -> list[dict]:
    out: list[dict] = []

    for item in you.get("purchase_log") or []:
        key = item.get("key", "")
        if key in KEY_ITEMS:
            out.append({"t": item["time"], "type": "item", "detail": f"Bought {KEY_ITEMS[key]}"})
        elif key == "smoke_of_deceit":
            out.append({"t": item["time"], "type": "smoke", "detail": "Bought Smoke (gank initiation)"})

    for b in you.get("buyback_log") or []:
        out.append({"t": b.get("time", 0), "type": "buyback", "detail": "Used buyback"})

    for w in you.get("obs_log") or []:
        out.append({"t": w.get("time", 0), "type": "ward_obs", "detail": "Placed observer ward"})
    for w in you.get("sen_log") or []:
        out.append({"t": w.get("time", 0), "type": "ward_sen", "detail": "Placed sentry ward"})

    user_hero_npc = heroes_by_id.get(you.get("hero_id"), {}).get("name", "")
    for p in match.get("players") or []:
        their_radiant = (p.get("player_slot") or 0) < 128
        if their_radiant == user_team_is_radiant:
            continue
        their_name = heroes_by_id.get(p.get("hero_id"), {}).get("localized_name", "?")
        for k in p.get("kills_log") or []:
            if k.get("key") == user_hero_npc:
                out.append({"t": k["time"], "type": "death", "detail": f"Died to {their_name}"})

    for k in you.get("kills_log") or []:
        victim = heroes_by_npc.get(k.get("key", ""), {}).get("localized_name", k.get("key", "?"))
        out.append({"t": k["time"], "type": "kill", "detail": f"Killed {victim}"})

    for o in match.get("objectives") or []:
        if o.get("type") != "CHAT_MESSAGE_ROSHAN_KILL":
            continue
        by_radiant = o.get("team") == _TOWER_TEAM_RADIANT
        if by_radiant == user_team_is_radiant:
            out.append({"t": o["time"], "type": "roshan", "detail": "Your team killed Roshan"})

    return out


def _cluster_deaths(decisions: list[dict], window_s: int = 30) -> list[dict]:
    """Merge death decisions occurring within `window_s` of each other into one cluster.

    Same fight = same penalty. Without this, dying 3 times in a 5v5 team fight gets
    flagged as 3 separate leaks even though it was one bad call.
    """
    deaths = sorted([d for d in decisions if d["type"] == "death"], key=lambda d: d["t"])
    others = [d for d in decisions if d["type"] != "death"]
    if len(deaths) < 2:
        return decisions

    merged: list[dict] = []
    cluster: list[dict] = []
    for d in deaths:
        if cluster and d["t"] - cluster[-1]["t"] <= window_s:
            cluster.append(d)
        else:
            if cluster:
                merged.append(_collapse(cluster))
            cluster = [d]
    if cluster:
        merged.append(_collapse(cluster))
    return others + merged


def _collapse(cluster: list[dict]) -> dict:
    if len(cluster) == 1:
        return cluster[0]
    enemies = sorted({d["detail"].replace("Died to ", "") for d in cluster})
    enemies_str = (
        ", ".join(enemies)
        if len(enemies) <= 3
        else f"{', '.join(enemies[:3])} +{len(enemies) - 3} more"
    )
    return {
        "t": cluster[0]["t"],
        "type": "death",
        "detail": f"Died {len(cluster)}× in team fight (to {enemies_str})",
        "cluster_size": len(cluster),
    }


def _score(decisions: list[dict], user_win_prob: list[float]) -> list[dict]:
    n = len(user_win_prob)
    for d in decisions:
        t = d["t"]
        before = max(0, min(n - 1, (t - 30) // 60))
        after = max(0, min(n - 1, (t + 90) // 60))
        d["impact"] = round(user_win_prob[after] - user_win_prob[before], 4)
        d["before_min"] = before
        d["after_min"] = after
    return decisions


_ACTION_TYPES = frozenset({"item", "ward_obs", "ward_sen", "smoke", "buyback"})


def _filter_implausible_attributions(decisions: list[dict]) -> list[dict]:
    """Drop attribution false-positives caused by Δwp window overlap.

    The scorer computes Δwp over `[t-30s, t+90s]`, so events within ~90s share
    the same window and get the same magnitude. That's mechanically correct but
    causally misleading — an item bought 20s after a death gets the death's
    full Δwp credit even though it didn't cause anything.

    Rules:
      - Hard (sign): a death with impact ≥ 0 makes no semantic sense; drop.
        Same for a kill/roshan with impact ≤ 0.
      - Soft (causal): for action events (item / ward / smoke / buyback),
        if a death is within 90s AND impact < 0 → drop (death is the cause).
        If a kill is within 90s AND impact > 0 → drop (kill is the cause).
    """
    death_times = [d["t"] for d in decisions if d["type"] == "death"]
    kill_times  = [d["t"] for d in decisions if d["type"] in ("kill", "roshan")]

    out: list[dict] = []
    for d in decisions:
        t, kind, impact = d["t"], d["type"], d["impact"]

        if kind == "death" and impact >= 0:
            continue
        if kind in ("kill", "roshan") and impact <= 0:
            continue

        if kind in _ACTION_TYPES:
            if impact < 0 and any(abs(t - dt) <= 90 for dt in death_times):
                continue
            if impact > 0 and any(abs(t - kt) <= 90 for kt in kill_times):
                continue

        out.append(d)
    return out


def _format_decision(d: dict, match_id: int) -> dict:
    out = {
        "t": _format_time(d["t"]),
        "type": d["type"],
        "impact": d["impact"],
        "detail": d["detail"],
        "replay_url": _replay_url(match_id, d["t"]),
    }
    if d.get("cluster_size"):
        out["cluster_size"] = d["cluster_size"]
    return out


def analyze(
    match_id: int,
    account_id: int | None = None,
    top_k: int = 5,
    min_impact: float = 0.005,
) -> dict:
    aid = config.resolve_account_id(account_id)
    try:
        match = fetcher.fetch_match(match_id)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            raise SystemExit(f"match {match_id} not found on OpenDota")
        if status and status >= 500:
            raise SystemExit(
                f"OpenDota is currently unreachable (HTTP {status}). Try again in a few minutes."
            )
        raise

    if match.get("game_mode") != config.TURBO_GAME_MODE:
        raise SystemExit(
            f"match {match_id} is not Turbo (game_mode={match.get('game_mode')}) — "
            "model is only valid in-distribution for Turbo"
        )
    if not fetcher.is_parsed(match):
        raise SystemExit(
            f"match {match_id} is not parsed yet — run `match-fetch` then "
            "`request-parses` and try again in a few minutes"
        )

    you = fetcher.player_for(match, aid)
    if not you:
        raise SystemExit(f"account_id {aid} is not in match {match_id}")

    user_team_is_radiant = (you.get("player_slot") or 0) < 128
    heroes_id_map = heroes_by_id()
    heroes_by_npc = _heroes_by_npc(heroes_id_map)
    model, features, calibrators = _load_model()

    snaps = snapshots.extract(match)
    if not snaps:
        raise SystemExit(f"could not extract snapshots from match {match_id}")

    match_rank = fetcher.avg_rank_tier(match)
    radiant_wp = _win_prob_curve(model, snaps, features, calibrators, match_rank)
    user_wp = radiant_wp if user_team_is_radiant else [1 - p for p in radiant_wp]

    raw_decisions = _extract_decisions(
        match, you, heroes_id_map, heroes_by_npc, user_team_is_radiant
    )
    clustered = _cluster_deaths(raw_decisions, window_s=30)
    scored = _score(clustered, user_wp)
    plausible = _filter_implausible_attributions(scored)
    filtered = [d for d in plausible if abs(d["impact"]) >= min_impact]
    leaks = sorted([d for d in filtered if d["impact"] < 0], key=lambda d: d["impact"])[:top_k]
    kept = sorted([d for d in filtered if d["impact"] > 0], key=lambda d: d["impact"], reverse=True)[:top_k]

    return {
        "match_id": match_id,
        "account_id": aid,
        "you": {
            "hero": heroes_id_map.get(you.get("hero_id"), {}).get("localized_name", "?"),
            "hero_id": you.get("hero_id"),
            "slot": you.get("player_slot"),
            "team": "radiant" if user_team_is_radiant else "dire",
            "kda": f"{you.get('kills', 0)}/{you.get('deaths', 0)}/{you.get('assists', 0)}",
            "lane_role": _LANE_ROLE.get(you.get("lane_role")) or "?",
            "result": "win" if bool(match["radiant_win"]) == user_team_is_radiant else "loss",
        },
        "match": {
            "patch": match.get("patch"),
            "avg_rank_tier": match_rank,
            "calibrated": (match_rank // 10 if match_rank else None) in calibrators,
        },
        "duration_min": len(snaps),
        "win_prob_curve": [round(p, 3) for p in user_wp],
        "decisions": {
            "biggest_leaks":   [_format_decision(d, match_id) for d in leaks],
            "kept_doing_this": [_format_decision(d, match_id) for d in kept],
        },
    }
