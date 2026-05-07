# dotaAnalytics

Personal Dota 2 Turbo decision analyzer. Pulls your match history via OpenDota, trains a turbo-specific win-probability model conditioned on your rank bracket, then ranks the decisions in any single match by their impact on win probability — so you can see which calls helped and which were leaks.

Scope: **Turbo only** (`game_mode = 23`), one user, runs locally via docker compose.

## Architecture

```
app  (python 3.12)              db  (postgres 16)
  ├─ CLI: fetch / train / ...     └─ matches, snapshots, decisions
  └─ later: Streamlit UI on :8501

shared volume: ./data           raw match JSON cache
```

## Quick start

Edit `.env` so `ACCOUNT_ID` is your Dota friend code, then:

```bash
docker compose build
docker compose run --rm app python -m app.cli account            # sanity check
docker compose run --rm app python -m app.cli fetch --limit 50   # start small
```

Postgres is started automatically by `depends_on` + healthcheck.

Inspect what landed:

```bash
docker compose exec db psql -U dota -d dota -c \
  "SELECT match_id, duration, parsed, avg_rank_tier,
          your_hero_id, your_kills, your_deaths, your_assists, radiant_win
   FROM matches ORDER BY start_time DESC LIMIT 10;"
```

## CLI

| Command | What it does |
|---|---|
| `python -m app.cli account` | Print the resolved account id |
| `python -m app.cli fetch --limit N` | Sync your N most recent Turbo matches — DB row + cached JSON per match |

`fetch` is idempotent: cached JSONs are reused, DB rows upserted on `match_id`.

## Configuration

Single `.env` file at the repo root:

```
ACCOUNT_ID=446619601     # your Dota friend code, or 32-bit OpenDota account id
```

`docker-compose.yml` injects `DATABASE_URL` and `DATA_DIR` automatically. Outside docker, set them yourself.

## Layout

```
.
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env(.example)
├── db/init.sql            # postgres schema
└── app/
    ├── config.py          # env-driven config
    ├── fetcher.py         # OpenDota client + cache + DB upsert
    └── cli.py             # entry points
```

## Roadmap

| Phase | Status | What |
|---|---|---|
| 1. Ingest | done | docker compose, postgres schema, OpenDota fetcher with cache |
| 2. Snapshots + training | next | per-minute feature rows, XGBoost win-prob model conditioned on rank |
| 3. Decision scorer |  | extract item buys / deaths / kills, score by Δ win-prob |
| 4. Streamlit UI |  | match_id → ranked decisions on a timeline |

## Approach (Tier 1)

Heuristic decision extraction + ML win-probability scorer:

1. Fetch ~5k parsed Turbo matches at your `avg_rank_tier ± 10` from OpenDota.
2. Build per-minute snapshots (`gold_adv`, `xp_adv`, tower / kill / rosh diffs, rank tier) and train an XGBoost classifier with `radiant_win` as the label.
3. For a given match, walk *your* purchase log, kill log, and death events to surface candidate decisions; score each by win-prob delta in a window around its timestamp.

There's no ground-truth label for "bad decision" in Dota — only win/loss — so this hybrid is the cheapest path to explainable output. Pure end-to-end ML (sequence/policy models) is in the much-bigger territory we explicitly skipped.

## Cost

$0 — runs locally. OpenDota's free tier (50k calls / month) covers the data volume. Disk: 5–15 GB for ~5k cached parsed matches. XGBoost trains on a laptop in minutes.
