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


def heroes_by_id() -> dict[int, dict]:
    """Return {hero_id: hero_dict} from cached /heroes endpoint."""
    path = config.DATA_DIR / "heroes.json"
    if not path.exists():
        path.write_text(json.dumps(fetcher._get("/heroes")))
    return {h["id"]: h for h in json.loads(path.read_text())}


def _heroes_by_npc(heroes: dict[int, dict]) -> dict[str, dict]:
    return {h["name"]: h for h in heroes.values()}


def _load_model() -> xgb.XGBClassifier:
    if not config.MODEL_PATH.exists():
        raise SystemExit(
            f"no model at {config.MODEL_PATH} — run `train` first "
            "(needs parsed snapshots in the DB)"
        )
    model = xgb.XGBClassifier()
    model.load_model(str(config.MODEL_PATH))
    return model


def _win_prob_curve(model: xgb.XGBClassifier, snaps: list[dict]) -> list[float]:
    X = np.array([[s[c] for c in FEATURE_COLS] for s in snaps], dtype=np.float32)
    return model.predict_proba(X)[:, 1].tolist()


def _format_time(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    s = abs(int(seconds))
    return f"{sign}{s // 60:02d}:{s % 60:02d}"


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


def _format_decision(d: dict) -> dict:
    return {
        "t": _format_time(d["t"]),
        "type": d["type"],
        "impact": d["impact"],
        "detail": d["detail"],
    }


def analyze(match_id: int, top_k: int = 5, min_impact: float = 0.005) -> dict:
    try:
        match = fetcher.fetch_match(match_id)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise SystemExit(f"match {match_id} not found on OpenDota")
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

    you = fetcher.your_player(match)
    if not you:
        raise SystemExit(
            f"account_id {config.require_account_id()} is not in match {match_id}"
        )

    user_team_is_radiant = (you.get("player_slot") or 0) < 128
    heroes_by_id = heroes_by_id()
    heroes_by_npc = _heroes_by_npc(heroes_by_id)
    model = _load_model()

    snaps = snapshots.extract(match)
    if not snaps:
        raise SystemExit(f"could not extract snapshots from match {match_id}")

    radiant_wp = _win_prob_curve(model, snaps)
    user_wp = radiant_wp if user_team_is_radiant else [1 - p for p in radiant_wp]

    decisions = _extract_decisions(
        match, you, heroes_by_id, heroes_by_npc, user_team_is_radiant
    )
    decisions = _score(decisions, user_wp)
    scored = [d for d in decisions if abs(d["impact"]) >= min_impact]
    scored.sort(key=lambda d: d["impact"])
    leaks = scored[:top_k]
    kept = list(reversed(scored[-top_k:]))

    return {
        "match_id": match_id,
        "you": {
            "hero": heroes_by_id.get(you.get("hero_id"), {}).get("localized_name", "?"),
            "slot": you.get("player_slot"),
            "team": "radiant" if user_team_is_radiant else "dire",
            "kda": f"{you.get('kills', 0)}/{you.get('deaths', 0)}/{you.get('assists', 0)}",
            "result": "win" if bool(match["radiant_win"]) == user_team_is_radiant else "loss",
        },
        "duration_min": len(snaps),
        "win_prob_curve": [round(p, 3) for p in user_wp],
        "decisions": {
            "biggest_leaks": [_format_decision(d) for d in leaks],
            "kept_doing_this": [_format_decision(d) for d in kept],
        },
    }
