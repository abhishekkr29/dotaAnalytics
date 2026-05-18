"""Destructive: wipe training data so we can rebuild from scratch.

Two modes:

  Default (training-data wipe)
    Drops:
      - DB: matches, snapshots, user_matches            (TRUNCATE … CASCADE)
      - data/matches/*.json                             (cached match JSONs)
      - data/turbo_winprob.json, calibrators.joblib,
        baselines.json, model_meta.json                 (model artifacts)
    Keeps:
      - users table + Anthropic BYO keys + cost counters
      - data/profiles/*.json
      - data/coach_memory/*.json
      - data/reviews/<aid>/*.md
      - data/heroes.json

  --full mode (factory reset — runs as a brand-new user)
    Everything above, PLUS:
      - users table rows                                (Steam IDs, cost counters wiped)
      - data/profiles/*.json
      - data/coach_memory/*.json
      - data/reviews/                                   (every user's reviews directory)
      - data/heroes.json
    Result: identical to a fresh checkout, except .env and the docker volume itself.

Usage:
    docker compose run --rm app python scripts/wipe_data.py --confirm
        # default mode, prompts for the word "wipe"
    docker compose run --rm app python scripts/wipe_data.py --confirm --full
        # full reset, prompts for the phrase "wipe everything"
    docker compose run --rm app python scripts/wipe_data.py --confirm --keep-matches-json
        # default mode but keep the cached match JSONs

Idempotent. Re-running on already-empty state is a no-op.
"""

import argparse
import shutil
import sys

from app import config, db


def _confirm_prompt(expected: str) -> bool:
    """Two-factor: requires both --confirm flag AND typed phrase at stdin."""
    if not sys.stdin.isatty():
        print(f"non-tty environment: skipping interactive prompt (expected: {expected!r})")
        return True
    answer = input(f"Type {expected!r} to proceed (anything else aborts): ").strip()
    return answer == expected


def _truncate_tables(tables: list[str]) -> dict:
    """TRUNCATE … CASCADE each table. Returns {table: rows_before_truncate}."""
    summary: dict = {}
    db.ensure_schema()
    with db.connect() as conn:
        for table in tables:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            before = int(row[0]) if row else 0
            conn.execute(f"TRUNCATE TABLE {table} CASCADE")
            summary[f"{table}_rows_dropped"] = before
    return summary


def _delete_files(paths) -> int:
    n = 0
    for p in paths:
        if p.exists():
            p.unlink()
            n += 1
    return n


def _empty_directory(dir_path) -> int:
    """Remove every entry under `dir_path` but keep the dir itself."""
    if not dir_path.exists():
        return 0
    n = 0
    for child in dir_path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true",
                        help="First gate. Required.")
    parser.add_argument("--full", action="store_true",
                        help="Factory reset — also wipe users, profiles, coach memory, "
                             "reviews, and heroes.json. Run if you want a brand-new-user "
                             "experience.")
    parser.add_argument("--keep-matches-json", action="store_true",
                        help="Skip deleting data/matches/*.json (DB rows still dropped). "
                             "Ignored in --full mode.")
    args = parser.parse_args()

    if not args.confirm:
        print("Refusing to run without --confirm. See `--help`.", file=sys.stderr)
        return 2

    if args.full:
        print(
            "FULL WIPE. This will drop:\n"
            "  - matches, snapshots, user_matches, users (DB tables)\n"
            "  - all cached match JSONs, profiles, coach memory, reviews\n"
            "  - trained model, calibrators, baselines, model_meta\n"
            "  - data/heroes.json\n"
            "What stays: .env, Postgres schema, the codebase itself.\n"
        )
        if not _confirm_prompt("wipe everything"):
            print("Aborted.")
            return 1
    else:
        print(
            "This will TRUNCATE matches, snapshots, user_matches and delete "
            "the trained model + calibrators + baselines + cached match JSONs.\n"
            "User accounts, coach memory, and reviews will be preserved "
            "(pass --full to wipe those too).\n"
        )
        if not _confirm_prompt("wipe"):
            print("Aborted.")
            return 1

    summary: dict = {}

    # ---- DB tables ----
    db_tables = ["user_matches", "snapshots", "matches"]
    if args.full:
        db_tables.append("users")
    summary.update(_truncate_tables(db_tables))

    # ---- Always: model + calibrators + baselines + metrics ----
    summary["artifact_files_dropped"] = _delete_files([
        config.MODEL_PATH,
        config.DATA_DIR / "calibrators.joblib",
        config.DATA_DIR / "baselines.json",
        config.DATA_DIR / "model_meta.json",
    ])

    # ---- Cached match JSONs (skipped if --keep-matches-json in default mode) ----
    if args.full or not args.keep_matches_json:
        n_json = 0
        if config.MATCHES_DIR.exists():
            for f in config.MATCHES_DIR.glob("*.json"):
                f.unlink()
                n_json += 1
        summary["match_jsons_dropped"] = n_json
    else:
        summary["match_jsons_dropped"] = "skipped (--keep-matches-json)"

    # ---- --full extras ----
    if args.full:
        summary["profiles_dropped"]      = _empty_directory(config.PROFILES_DIR)
        summary["memory_files_dropped"]  = _empty_directory(config.MEMORY_DIR)
        summary["reviews_dropped"]       = _empty_directory(config.REVIEWS_DIR)
        summary["heroes_json_dropped"]   = _delete_files([config.DATA_DIR / "heroes.json"])

    print()
    print("Wipe complete." if not args.full else "Full wipe complete — running as a fresh user.")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print()
    print("Next steps:")
    print("  1. bash scripts/collect_data.sh    # gather across 7 brackets")
    print("  2. bash scripts/train_model.sh     # build model + calibrators + baselines")
    if args.full:
        print()
        print("Sign in to the web UI to recreate your user account (Steam OpenID or "
              "$ACCOUNT_ID env fallback).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
