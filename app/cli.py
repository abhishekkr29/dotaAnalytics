import argparse
import json

from app import config, fetcher


def cmd_fetch(args: argparse.Namespace) -> None:
    print(json.dumps(fetcher.sync_player_matches(limit=args.limit), indent=2))


def cmd_account(_: argparse.Namespace) -> None:
    print(f"account_id: {config.require_account_id()}")


def main() -> None:
    p = argparse.ArgumentParser(prog="dota-analytics")
    sub = p.add_subparsers(required=True)

    f = sub.add_parser("fetch", help="Sync your turbo match history into the DB + cache")
    f.add_argument("--limit", type=int, default=200)
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("account", help="Show resolved account id")
    a.set_defaults(func=cmd_account)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
