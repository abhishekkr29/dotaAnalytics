"""Counterfactual baselines: per-(rank_bucket, hero, item) median purchase time.

Lets the coach say "your BKB at 17:30 — 3.5 min late vs Crusader Storm median 14:00"
without per-bracket models. The baselines are computed once from the cached parsed
match JSONs and saved to data/baselines.json.

Schema (data/baselines.json):
{
  "generated_at": "2026-05-17T...",
  "n_matches_scanned": 997,
  "n_samples": 18432,
  "samples": {
    "<rank_bucket>|<hero_id>|<item_key>": {
      "median_seconds": int,
      "p25_seconds": int,
      "p75_seconds": int,
      "n_samples": int
    },
    ...
  }
}

`rank_bucket = avg_rank_tier // 10` → 1=Herald, 2=Guardian, ..., 7=Divine, 8=Immortal.
"""

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from tqdm import tqdm

from app import config, fetcher
from app.analyze import KEY_ITEMS

_NOISE_FLOOR = 5  # need at least N samples to publish a median


def _rank_bucket(rt: int | None) -> int | None:
    if rt is None or rt < 10:
        return None
    return rt // 10


def build() -> dict:
    """Scan every cached parsed match, compute medians, write data/baselines.json."""
    samples: dict[tuple[int, int, str], list[int]] = defaultdict(list)
    matches_scanned = 0

    for cache in tqdm(list(config.MATCHES_DIR.glob("*.json")), desc="baselines"):
        try:
            match = json.loads(cache.read_text())
        except json.JSONDecodeError:
            continue
        if not fetcher.is_parsed(match):
            continue
        rt = fetcher.avg_rank_tier(match)
        bucket = _rank_bucket(rt)
        if bucket is None:
            continue
        matches_scanned += 1
        for p in match.get("players") or []:
            hero = p.get("hero_id")
            if not hero:
                continue
            for item in (p.get("purchase_log") or []):
                key = item.get("key", "")
                if key in KEY_ITEMS and item.get("time") is not None:
                    samples[(bucket, hero, key)].append(int(item["time"]))

    out_samples = {}
    for (bucket, hero, key), times in samples.items():
        if len(times) < _NOISE_FLOOR:
            continue
        s = sorted(times)
        n = len(s)
        out_samples[f"{bucket}|{hero}|{key}"] = {
            "median_seconds": int(statistics.median(s)),
            "p25_seconds":    int(s[n // 4]),
            "p75_seconds":    int(s[(3 * n) // 4]),
            "n_samples":      n,
        }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_matches_scanned": matches_scanned,
        "n_samples_total": sum(len(v) for v in samples.values()),
        "samples": out_samples,
    }
    _path().write_text(json.dumps(payload, indent=2))
    return {
        "matches_scanned": matches_scanned,
        "buckets_published": len(out_samples),
        "buckets_dropped_below_noise_floor": sum(1 for v in samples.values() if len(v) < _NOISE_FLOOR),
        "path": str(_path()),
    }


def _path():
    return config.DATA_DIR / "baselines.json"


def load() -> dict:
    """Load baselines, or return an empty stub if not built yet."""
    p = _path()
    if not p.exists():
        return {"samples": {}}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"samples": {}}


def lookup(baselines: dict, rank_tier: int | None, hero_id: int, item_key: str) -> dict | None:
    """Resolve the baseline for a specific (rank, hero, item). Returns None if not published."""
    bucket = _rank_bucket(rank_tier)
    if bucket is None:
        return None
    key = f"{bucket}|{hero_id}|{item_key}"
    return baselines.get("samples", {}).get(key)


def format_delta(observed_seconds: int, baseline: dict) -> str:
    """Format `observed vs baseline` for prompt injection.

    Examples:
      "BKB@17:30 (baseline 14:00, +3:30 late, n=84)"
      "BKB@13:10 (baseline 14:00, -0:50 early, n=84)"
    """
    median = baseline["median_seconds"]
    delta = observed_seconds - median
    sign = "+" if delta >= 0 else "-"
    label = "late" if delta > 30 else ("early" if delta < -30 else "on-time")
    return (
        f"observed {_mmss(observed_seconds)} "
        f"(bracket median {_mmss(median)}, {sign}{_mmss(abs(delta))} {label}, "
        f"n={baseline['n_samples']})"
    )


def _mmss(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
