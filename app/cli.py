import argparse
import json

from app import config, db, fetcher


def _account(args: argparse.Namespace) -> int:
    """Resolve account id for any subcommand: --account beats $ACCOUNT_ID."""
    return config.resolve_account_id(getattr(args, "account", None))


def cmd_account(args: argparse.Namespace) -> None:
    print(f"account_id: {_account(args)}")


def cmd_profile(args: argparse.Namespace) -> None:
    p = fetcher.fetch_profile(_account(args), force=args.refresh)
    print(json.dumps({
        "account_id": p.get("profile", {}).get("account_id"),
        "personaname": p.get("profile", {}).get("personaname"),
        "rank_tier": p.get("rank_tier"),
        "computed_mmr_turbo": p.get("computed_mmr_turbo"),
    }, indent=2))


def cmd_bracket_fetch(args: argparse.Namespace) -> None:
    print(json.dumps(
        fetcher.sync_bracket_matches(
            account_id=_account(args),
            limit=args.limit,
            window=args.window,
            rank_tier=args.rank,
        ),
        indent=2,
    ))


def cmd_match_fetch(args: argparse.Namespace) -> None:
    print(json.dumps(
        fetcher.sync_match(args.match_id, account_id=_account(args)),
        indent=2,
    ))


def cmd_request_parses(args: argparse.Namespace) -> None:
    # Account-agnostic — operates on global matches table.
    print(json.dumps(
        fetcher.request_parses(
            limit=args.limit,
            rank_min=args.rank_min,
            rank_max=args.rank_max,
        ),
        indent=2,
    ))


def cmd_refresh_parses(args: argparse.Namespace) -> None:
    print(json.dumps(
        fetcher.refresh_parses(
            limit=args.limit,
            account_id=_account(args),
            mode=args.mode,
            fallback_after_hours=args.fallback_after_hours,
        ),
        indent=2,
    ))


def cmd_snapshots(args: argparse.Namespace) -> None:
    from app import snapshots
    print(json.dumps(snapshots.build_all(only_missing=not args.rebuild), indent=2))


def cmd_train(args: argparse.Namespace) -> None:
    from app import train
    print(json.dumps(train.train(n_estimators=args.n_estimators), indent=2))


def cmd_refresh_doc(_: argparse.Namespace) -> None:
    from app import train
    print(json.dumps(train.refresh_doc_from_meta(), indent=2))


def cmd_training_status(_: argparse.Namespace) -> None:
    from app import train
    print(json.dumps(train.parsed_count_since_train(), indent=2))


def cmd_build_baselines(_: argparse.Namespace) -> None:
    from app import baselines
    print(json.dumps(baselines.build(), indent=2))


def cmd_sweep(args: argparse.Namespace) -> None:
    from scripts import sweep
    print(json.dumps(sweep.run(out_path=args.out), indent=2))


def cmd_analyze(args: argparse.Namespace) -> None:
    from app import analyze
    print(json.dumps(
        analyze.analyze(
            args.match_id, account_id=_account(args),
            top_k=args.top_k, min_impact=args.min_impact,
        ),
        indent=2,
    ))


def cmd_coach(args: argparse.Namespace) -> None:
    import sys
    from app import coach

    on_chunk = None
    if args.stream:
        def on_chunk(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()

    result = coach.coach(
        args.match_id, account_id=_account(args),
        model=args.model, top_k=args.top_k, min_impact=args.min_impact,
        on_chunk=on_chunk,
    )
    if args.stream:
        sys.stdout.write("\n\n")
    print(json.dumps(result, indent=2))


def _account_parent() -> argparse.ArgumentParser:
    """Reusable parent parser supplying --account to subcommands that need it."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--account", type=int, default=None,
        help="Account ID to operate against (defaults to $ACCOUNT_ID)",
    )
    return parent


def main() -> None:
    db.ensure_schema()

    p = argparse.ArgumentParser(prog="dota-analytics")
    sub = p.add_subparsers(required=True)
    acct = _account_parent()

    a = sub.add_parser("account", help="Show the resolved account id", parents=[acct])
    a.set_defaults(func=cmd_account)

    pr = sub.add_parser(
        "profile", help="Fetch (and cache) a player's OpenDota profile + rank",
        parents=[acct],
    )
    pr.add_argument("--refresh", action="store_true")
    pr.set_defaults(func=cmd_profile)

    bf = sub.add_parser(
        "bracket-fetch", help="Pull turbo matches at a rank bracket via /explorer",
        parents=[acct],
    )
    bf.add_argument("--limit", type=int, default=500)
    bf.add_argument("--window", type=int, default=10)
    bf.add_argument(
        "--rank", type=int, default=None,
        help="Target rank_tier (default: signed-in account's own rank from profile). "
             "Use to bootstrap training data across brackets, e.g. --rank 15 for Herald.",
    )
    bf.set_defaults(func=cmd_bracket_fetch)

    mf = sub.add_parser(
        "match-fetch", help="Fetch one match by ID and upsert it",
        parents=[acct],
    )
    mf.add_argument("match_id", type=int)
    mf.set_defaults(func=cmd_match_fetch)

    rq = sub.add_parser("request-parses", help="Submit parse requests for unparsed matches in DB")
    rq.add_argument("--limit", type=int, default=200)
    rq.add_argument(
        "--rank-min", type=int, default=None,
        help="Only request parses for matches with avg_rank_tier >= this (inclusive)",
    )
    rq.add_argument(
        "--rank-max", type=int, default=None,
        help="Only request parses for matches with avg_rank_tier <= this (inclusive)",
    )
    rq.set_defaults(func=cmd_request_parses)

    rf = sub.add_parser(
        "refresh-parses", help="Pick up any unparsed matches that finished parsing",
        parents=[acct],
    )
    rf.add_argument("--limit", type=int, default=200)
    rf.add_argument(
        "--mode", default="explorer",
        choices=["explorer", "per_match", "hybrid"],
        help="explorer (default, cheapest): batch status via /explorer + JSON fetch only "
             "for confirmed-parsed. ~50x fewer API calls. per_match (legacy): GET /matches/{id} "
             "for every unparsed row. hybrid: explorer-batch + per-match fallback for matches "
             "pending >--fallback-after-hours.",
    )
    rf.add_argument(
        "--fallback-after-hours", type=int, default=48,
        help="Hybrid mode: per-match fallback only for matches pending this long without "
             "/explorer confirmation. Ignored in explorer / per_match modes.",
    )
    rf.set_defaults(func=cmd_refresh_parses)

    sn = sub.add_parser("snapshots", help="Extract per-minute training rows from parsed matches")
    sn.add_argument("--rebuild", action="store_true", help="Rebuild all (default: only missing)")
    sn.set_defaults(func=cmd_snapshots)

    tr = sub.add_parser("train", help="Train the XGBoost win-prob model on snapshots")
    tr.add_argument("--n-estimators", type=int, default=400)
    tr.set_defaults(func=cmd_train)

    rd = sub.add_parser(
        "refresh-doc",
        help="Re-sync docs/TRAINING.md from existing data/model_meta.json (no retrain)",
    )
    rd.set_defaults(func=cmd_refresh_doc)

    ts = sub.add_parser(
        "training-status",
        help="Show how many new parsed matches have accumulated since the last train",
    )
    ts.set_defaults(func=cmd_training_status)

    bb = sub.add_parser(
        "build-baselines",
        help="Compute per-(rank, hero, item) median purchase times from cached match JSONs",
    )
    bb.set_defaults(func=cmd_build_baselines)

    sw = sub.add_parser(
        "sweep",
        help="Run a small XGBoost hyperparameter sweep and print results (no artifact saved)",
    )
    sw.add_argument("--out", type=str, default=None, help="Optional path to write sweep results JSON")
    sw.set_defaults(func=cmd_sweep)

    an = sub.add_parser(
        "analyze", help="Rank a match's decisions by win-prob delta",
        parents=[acct],
    )
    an.add_argument("match_id", type=int)
    an.add_argument("--top-k", type=int, default=5, help="Top-K leaks and kept-doing items")
    an.add_argument("--min-impact", type=float, default=0.005, help="Drop decisions below this |Δ win-prob|")
    an.set_defaults(func=cmd_analyze)

    co = sub.add_parser(
        "coach",
        help="Natural-language coach review via Claude (writes data/reviews/<account_id>/<id>.md)",
        parents=[acct],
    )
    co.add_argument("match_id", type=int)
    co.add_argument("--model", default="sonnet", choices=["haiku", "sonnet", "opus"])
    co.add_argument("--top-k", type=int, default=6)
    co.add_argument("--min-impact", type=float, default=0.005)
    co.add_argument(
        "--stream", action="store_true",
        help="Stream the review live to stdout (useful with --model opus due to latency)",
    )
    co.set_defaults(func=cmd_coach)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
