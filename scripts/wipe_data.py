"""Destructive: wipe user/history data. Parsed match data is preserved by default.

Default mode — USER WIPE (cheap to rebuild)
  Drops:
    - DB: users + user_matches  (cascade — users have FKs to user_matches)
    - data/profiles/<aid>.json  (per-user profile cache)
    - data/coach_memory/<aid>.json
    - data/reviews/<aid>/       (every user's review directory)
  Keeps (expensive to rebuild):
    - DB: matches, snapshots                (the training dataset itself)
    - data/matches/<id>.json                (cached parsed match JSONs)
    - data/turbo_winprob.json               (trained model)
    - data/calibrators.joblib               (per-bracket calibrators)
    - data/baselines.json                   (counterfactual baselines)
    - data/model_meta.json                  (training metrics)
    - data/heroes.json                      (hero metadata)

  Result: sign-in state and review history are gone; the user re-signs in via
  Steam OpenID and the system still has the full trained model + 15k+ parsed
  matches. They'll just need to re-bracket-fetch to populate THEIR user_matches
  links.

--include-matches — NUCLEAR WIPE (rare; only when training data is bad)
  Default mode PLUS:
    - DB: matches, snapshots
    - data/matches/*.json
    - data/turbo_winprob.json, calibrators.joblib, baselines.json, model_meta.json
    - data/heroes.json
  Result: identical to a fresh checkout (modulo .env). Requires re-running
  collect_data.sh + train_model.sh which costs ~$1.50 and ~5-10 min wall-clock
  with Premium key. Don't use this unless you actually need to.

Usage:
    docker compose run --rm app python scripts/wipe_data.py --confirm
        # USER WIPE — preserves parsed matches + model; prompts for "wipe"
    docker compose run --rm app python scripts/wipe_data.py --confirm --include-matches
        # NUCLEAR WIPE — full reset; prompts for "wipe everything"

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
    parser = argparse.ArgumentParser(
        description="Wipe user/history data (default) or full reset (--include-matches).",
    )
    parser.add_argument("--confirm", action="store_true",
                        help="First gate. Required.")
    parser.add_argument("--include-matches", action="store_true",
                        help="NUCLEAR: also wipe parsed matches, snapshots, model, calibrators, "
                             "baselines, match JSONs, heroes.json. Forces a full re-collect + "
                             "re-train (~$1.50, hours of work). Default leaves match data intact.")
    args = parser.parse_args()

    if not args.confirm:
        print("Refusing to run without --confirm. See `--help`.", file=sys.stderr)
        return 2

    if args.include_matches:
        print(
            "NUCLEAR WIPE. This will drop:\n"
            "  - users, user_matches, matches, snapshots\n"
            "  - profiles, coach memory, reviews\n"
            "  - trained model + calibrators + baselines + match JSONs + heroes.json\n"
            "Recovering requires re-running collect_data.sh + train_model.sh\n"
            "  (~$1.50 in OpenDota Premium calls and several minutes of compute).\n"
            "What stays: .env, Postgres schema, the codebase itself.\n"
        )
        if not _confirm_prompt("wipe everything"):
            print("Aborted.")
            return 1
    else:
        print(
            "USER WIPE. This will drop:\n"
            "  - users + user_matches (DB tables)\n"
            "  - data/profiles/*.json\n"
            "  - data/coach_memory/*.json\n"
            "  - data/reviews/<aid>/ (every user's reviews directory)\n"
            "Parsed match data, the trained model, calibrators, and baselines are KEPT.\n"
            "(Pass --include-matches to also wipe those — destructive and expensive.)\n"
        )
        if not _confirm_prompt("wipe"):
            print("Aborted.")
            return 1

    summary: dict = {}

    # ---- USER/HISTORY DB (always wiped) ----
    user_tables = ["user_matches", "users"]
    summary.update(_truncate_tables(user_tables))

    # ---- USER/HISTORY filesystem (always wiped) ----
    summary["profiles_dropped"]     = _empty_directory(config.PROFILES_DIR)
    summary["memory_files_dropped"] = _empty_directory(config.MEMORY_DIR)
    summary["reviews_dropped"]      = _empty_directory(config.REVIEWS_DIR)

    # ---- NUCLEAR extras (only with --include-matches) ----
    if args.include_matches:
        match_tables = ["snapshots", "matches"]
        summary.update(_truncate_tables(match_tables))

        summary["artifact_files_dropped"] = _delete_files([
            config.MODEL_PATH,
            config.DATA_DIR / "calibrators.joblib",
            config.DATA_DIR / "baselines.json",
            config.DATA_DIR / "model_meta.json",
            config.DATA_DIR / "heroes.json",
        ])

        n_json = 0
        if config.MATCHES_DIR.exists():
            for f in config.MATCHES_DIR.glob("*.json"):
                f.unlink()
                n_json += 1
        summary["match_jsons_dropped"] = n_json

    print()
    print(
        "Nuclear wipe complete — running as a fresh checkout."
        if args.include_matches
        else "User wipe complete — parsed match data and model preserved."
    )
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print()
    print("Next steps:")
    if args.include_matches:
        print("  1. bash scripts/collect_data.sh    # gather across 7 brackets")
        print("  2. bash scripts/train_model.sh     # build model + calibrators + baselines")
    else:
        print("  - Sign in to the web UI via Steam OpenID")
        print("  - The trained model + baselines are still in place; nothing to retrain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
