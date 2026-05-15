import argparse
import json

from app import config, db, fetcher


def cmd_account(_: argparse.Namespace) -> None:
    print(f"account_id: {config.require_account_id()}")


def cmd_profile(args: argparse.Namespace) -> None:
    p = fetcher.fetch_profile(force=args.refresh)
    print(json.dumps({
        "account_id": p.get("profile", {}).get("account_id"),
        "personaname": p.get("profile", {}).get("personaname"),
        "rank_tier": p.get("rank_tier"),
        "computed_mmr_turbo": p.get("computed_mmr_turbo"),
    }, indent=2))


def cmd_bracket_fetch(args: argparse.Namespace) -> None:
    print(json.dumps(
        fetcher.sync_bracket_matches(limit=args.limit, window=args.window),
        indent=2,
    ))


def cmd_match_fetch(args: argparse.Namespace) -> None:
    print(json.dumps(fetcher.sync_match(args.match_id), indent=2))


def cmd_request_parses(args: argparse.Namespace) -> None:
    print(json.dumps(fetcher.request_parses(limit=args.limit), indent=2))


def cmd_refresh_parses(args: argparse.Namespace) -> None:
    print(json.dumps(fetcher.refresh_parses(limit=args.limit), indent=2))


def cmd_snapshots(args: argparse.Namespace) -> None:
    from app import snapshots
    print(json.dumps(snapshots.build_all(only_missing=not args.rebuild), indent=2))


def cmd_train(args: argparse.Namespace) -> None:
    from app import train
    print(json.dumps(train.train(n_estimators=args.n_estimators), indent=2))


def cmd_refresh_doc(_: argparse.Namespace) -> None:
    from app import train
    print(json.dumps(train.refresh_doc_from_meta(), indent=2))


def cmd_analyze(args: argparse.Namespace) -> None:
    from app import analyze
    print(json.dumps(
        analyze.analyze(args.match_id, top_k=args.top_k, min_impact=args.min_impact),
        indent=2,
    ))


def cmd_coach(args: argparse.Namespace) -> None:
    from app import coach
    print(json.dumps(
        coach.coach(args.match_id, model=args.model, top_k=args.top_k, min_impact=args.min_impact),
        indent=2,
    ))


def main() -> None:
    db.ensure_schema()

    p = argparse.ArgumentParser(prog="dota-analytics")
    sub = p.add_subparsers(required=True)

    a = sub.add_parser("account", help="Show the resolved account id")
    a.set_defaults(func=cmd_account)

    pr = sub.add_parser("profile", help="Fetch (and cache) your OpenDota profile + rank")
    pr.add_argument("--refresh", action="store_true")
    pr.set_defaults(func=cmd_profile)

    bf = sub.add_parser("bracket-fetch", help="Pull turbo matches at your rank bracket via /explorer")
    bf.add_argument("--limit", type=int, default=500)
    bf.add_argument("--window", type=int, default=10)
    bf.set_defaults(func=cmd_bracket_fetch)

    mf = sub.add_parser("match-fetch", help="Fetch one match by ID and upsert it")
    mf.add_argument("match_id", type=int)
    mf.set_defaults(func=cmd_match_fetch)

    rq = sub.add_parser("request-parses", help="Submit parse requests for unparsed matches in DB")
    rq.add_argument("--limit", type=int, default=200)
    rq.set_defaults(func=cmd_request_parses)

    rf = sub.add_parser("refresh-parses", help="Re-fetch unparsed matches; pick up any that finished parsing")
    rf.add_argument("--limit", type=int, default=200)
    rf.set_defaults(func=cmd_refresh_parses)

    sn = sub.add_parser("snapshots", help="Extract per-minute training rows from parsed matches")
    sn.add_argument("--rebuild", action="store_true", help="Rebuild all (default: only missing)")
    sn.set_defaults(func=cmd_snapshots)

    tr = sub.add_parser("train", help="Train the XGBoost win-prob model on snapshots")
    tr.add_argument("--n-estimators", type=int, default=400)
    tr.set_defaults(func=cmd_train)

    rd = sub.add_parser("refresh-doc", help="Re-sync docs/TRAINING.md from existing data/model_meta.json (no retrain)")
    rd.set_defaults(func=cmd_refresh_doc)

    an = sub.add_parser("analyze", help="Rank a match's decisions by win-prob delta")
    an.add_argument("match_id", type=int)
    an.add_argument("--top-k", type=int, default=5, help="Top-K leaks and kept-doing items")
    an.add_argument("--min-impact", type=float, default=0.005, help="Drop decisions below this |Δ win-prob|")
    an.set_defaults(func=cmd_analyze)

    co = sub.add_parser("coach", help="Natural-language coach review via Claude (writes data/reviews/<id>.md)")
    co.add_argument("match_id", type=int)
    co.add_argument("--model", default="sonnet", choices=["haiku", "sonnet", "opus"], help="Claude model to use")
    co.add_argument("--top-k", type=int, default=6)
    co.add_argument("--min-impact", type=float, default=0.005)
    co.set_defaults(func=cmd_coach)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
