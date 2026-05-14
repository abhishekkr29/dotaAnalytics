# Architecture

## High-level diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  docker compose                                                 │
│                                                                 │
│   ┌──────────────────────┐         ┌──────────────────────┐     │
│   │  app  (python 3.12)  │         │  db  (postgres 16)   │     │
│   │                      │         │                      │     │
│   │  CLI subcommands     │◄───────►│  matches             │     │
│   │  · fetch / discover  │         │  snapshots           │     │
│   │  · train             │         │                      │     │
│   │  · analyze           │         └──────────────────────┘     │
│   │  · coach   ──────────┼──────► Anthropic Claude API          │
│   │  · …                 │                                      │
│   └──────────┬───────────┘                                      │
│              │                                                  │
│              │ shared volume                                    │
│              ▼                                                  │
│   ┌──────────────────────┐                                      │
│   │  ./data              │                                      │
│   │  · matches/*.json    │  raw match JSON cache (on disk)      │
│   │  · profile.json      │                                      │
│   │  · heroes.json       │                                      │
│   │  · turbo_winprob.json│  trained model artifact              │
│   │  · model_meta.json   │  training metrics                    │
│   └──────────────────────┘                                      │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
                   ┌─────────────────┐
                   │  OpenDota API   │
                   │ (free tier)     │
                   └─────────────────┘
```

Two services, one shared volume, one external dependency (OpenDota's public API).

## Module breakdown

| Module | Purpose |
|---|---|
| `app/config.py` | Reads env (`ACCOUNT_ID`, `DATABASE_URL`, `DATA_DIR`), exposes constants and `require_account_id()` |
| `app/db.py` | Postgres connection helper + idempotent `ensure_schema()` — single source of truth for schema |
| `app/fetcher.py` | OpenDota HTTP client with 429 retry. Profile, bracket discovery, match fetching, parse requests/refreshes, DB upsert |
| `app/snapshots.py` | Pure transform: parsed match JSON → list of per-minute snapshot dicts. Also writes them to the `snapshots` table |
| `app/train.py` | Loads snapshots, group-aware train/val split, fits XGBoost classifier, saves model + metrics |
| `app/analyze.py` | Loads a single match + model, computes win-prob curve, extracts and scores candidate decisions |
| `app/coach.py` | Hybrid layer on top of `analyze`: rule-based narrative beats + Claude (via `anthropic` SDK) to synthesize a markdown coach review |
| `app/cli.py` | argparse entry points. Calls `db.ensure_schema()` once on startup, dispatches to module functions |

## Training pipeline

```
  bracket-fetch                request-parses / refresh-parses              snapshots                 train
  ─────────────►               ──────────────────────────────►              ──────────►               ──────►
  /explorer query              POST /request/{id} → wait                    extract per-minute        XGBoost
  on public_matches            → GET /matches/{id}                          rows from each            classifier on
  at your rank ± W             until version IS NOT NULL                    parsed match              snapshots
  → matches table              → matches.parsed = true                      → snapshots table         → MODEL_PATH
```

| Step | Reads | Writes |
|---|---|---|
| `bracket-fetch` | `/explorer`, `/matches/{id}` | `matches` table, `data/matches/*.json` |
| `request-parses` | `matches` (unparsed) | OpenDota parse queue |
| `refresh-parses` | `matches` (unparsed), `/matches/{id}` | `matches.parsed` flips for any newly parsed |
| `snapshots` | `matches` (parsed), `data/matches/*.json` | `snapshots` table |
| `train` | `snapshots` | `data/turbo_winprob.json`, `data/model_meta.json` |

## Inference pipeline (`analyze`)

```
  match_id ──► fetch_match (cached if available)
                     │
                     ▼
              validate: Turbo + parsed + you're in players[]
                     │
                     ▼
              snapshots.extract() in memory (no DB write)
                     │
                     ▼
              model.predict_proba on each minute
                     │
                     ▼
              win-prob curve (flipped for Dire)
                     │
                     ▼
              extract decisions: items / deaths / kills / Roshan
                     │
                     ▼
              score each: Δ win-prob in (t−30s, t+90s)
                     │
                     ▼
              filter |impact| < min_impact, rank, top-K leaks + kept
                     │
                     ▼
              JSON report
```

`analyze` never writes to the DB. The same `snapshots.extract()` used at training time is reused in-memory at inference — single source of feature truth.

## The win-prob model

For the full feature schema, hyperparameter rationale, success thresholds, retraining triggers, and gotchas, see [TRAINING.md](TRAINING.md). The summary below is the architectural shape.

Per-minute features (one row per match-minute, see `app/train.py:FEATURE_COLS`):

| Column | Meaning |
|---|---|
| `minute` | Game time in minutes |
| `gold_adv` | Radiant gold − Dire gold (signed) |
| `xp_adv` | Radiant XP − Dire XP (signed) |
| `tower_kills_radiant` | Cumulative towers killed by Radiant |
| `tower_kills_dire` | Cumulative towers killed by Dire |
| `kills_radiant` | Cumulative hero kills by Radiant |
| `kills_dire` | Cumulative hero kills by Dire |
| `roshan_kills` | Cumulative Roshan kills (either team) |
| `avg_rank_tier` | Match's average rank tier — **the skill-bracket conditioning** |
| `r_hero_1..5`, `d_hero_1..5` | Hero IDs per side, sorted within team. Lets the model condition on composition and matchups. |

**Label:** `radiant_win` (boolean, same for every row from the same match).

**Model:** `xgb.XGBClassifier`, `tree_method="hist"`, max_depth=6, lr=0.05, early-stopping at 20 rounds. Train/val split is **group-aware by `match_id`** (`GroupShuffleSplit`) — snapshots from the same match never leak between train and val.

## Decision extraction

`analyze` walks four sources for the target player (you) and scores each candidate with Δ win-prob in a `(t−30s, t+90s)` window.

| Decision type | Source | Filter |
|---|---|---|
| Key item bought | `players[you].purchase_log` | `key ∈ KEY_ITEMS` in `app/analyze.py` |
| Death | `players[opponents].kills_log[].key == npc_dota_hero_<yours>` | all entries |
| Kill | `players[you].kills_log` | all entries |
| Roshan kill | `objectives[type == CHAT_MESSAGE_ROSHAN_KILL]` | only credited to your team |
| Smoke (gank init) | `players[you].purchase_log` with `key == "smoke_of_deceit"` | all entries |
| Observer ward | `players[you].obs_log` | all entries |
| Sentry ward | `players[you].sen_log` | all entries |

After scoring, decisions with `|impact| < min_impact` (default `0.005`) are dropped as noise. The rest are sorted by signed impact: top-K positive → "kept doing this", bottom-K → "biggest leaks".

The window is asymmetric (30s before, 90s after) because Dota decisions usually pay off in the next ~60s — a BKB bought now matters in the next fight, not the previous one.

## Database schema

Single source: `app/db.py:SCHEMA_SQL`. Runs idempotently at the start of every CLI command.

### `matches`

| column | type | notes |
|---|---|---|
| `match_id` | BIGINT PK | OpenDota match ID |
| `start_time` | BIGINT | epoch seconds |
| `duration` | INTEGER | seconds |
| `game_mode` | INTEGER | always 23 for our pipeline |
| `lobby_type` | INTEGER | public matchmaking values |
| `radiant_win` | BOOLEAN | training label |
| `avg_rank_tier` | INTEGER | match's average rank tier |
| `parsed` | BOOLEAN | true once `version` is set in match JSON |
| `patch` | INTEGER | informational |
| `your_slot`, `your_hero_id`, `your_kills`, `your_deaths`, `your_assists` | various | populated only if you played in this match |
| `fetched_at` | TIMESTAMPTZ | last upsert time |

Indexes: `parsed`, `avg_rank_tier`.

### `snapshots`

| column | type | notes |
|---|---|---|
| `(match_id, minute)` | BIGINT + INT PK | composite |
| feature columns | various | see model input table above |
| `radiant_win` | BOOLEAN | denormalised label so training is one SELECT |

Indexes: `avg_rank_tier`. FK `match_id → matches(match_id)` with `ON DELETE CASCADE`.

## Caching strategy

- **OpenDota match JSON** → `data/matches/{match_id}.json`. Always read from disk first; only hit the API on miss or `force=True`. Unparsed matches are NOT cached (no useful timeline data yet) — discovery records them in the `matches` table only.
- **Profile** → `data/profile.json` (1 call per refresh).
- **Heroes list** → `data/heroes.json` (1 call ever; refresh manually if new heroes ship).
- **Model artifact** → `data/turbo_winprob.json` (XGBoost native JSON format).
- **Training metrics** → `data/model_meta.json`.
- **Coach reviews** → `data/reviews/<match_id>.md` (one markdown file per `coach` invocation).
- **Coach session memory** → `data/coach_memory.json` (last 20 reviewed matches with heuristic themes; last 5 injected into the next coach prompt).

The DB stores summary rows; disk cache stores full JSON. Snapshots can always be rebuilt from the disk cache.

## Coach pipeline (Hybrid: rules + LLM)

Architectural shape only — for the full reference (beats schema, prompt design, session memory, tunables, cost details, gotchas), see [COACH.md](COACH.md).

```
analyze(match_id) → beats (rules) → prompt + injected memory → Claude → markdown review
                                                                              │
                                                                              ├─► data/reviews/<id>.md
                                                                              └─► data/coach_memory.json
```

**Architectural choices:**
- Rules pin facts, LLM phrases them. System prompt forbids invented events.
- Default model is Claude Sonnet 4.6. `--model haiku|opus` swaps tier.
- `ANTHROPIC_API_KEY` from env only; never logged or persisted.
- Session memory: `data/coach_memory.json` carries the last 20 reviews; the last 5 are injected into each new prompt for recurring-pattern detection.

## Why this design

- **No ground-truth label for "bad decision."** Only win/loss. We use heuristic decision *surfacing* + ML *scoring*.
- **Rank-conditioned model.** `avg_rank_tier` as a feature lets the same game state mean different things at different brackets.
- **Group-aware split.** Train/val split by `match_id` prevents intra-match leakage.
- **XGBoost over deep nets.** For tabular per-minute features XGBoost is competitive with sequence models, trains in seconds on a laptop, and is interpretable enough to debug.
- **In-memory extraction at analyze time.** Avoids DB roundtrip for one-off analyses.
- **Idempotent schema on every CLI run.** Schema evolution is a code change, not a manual migration.
- **Hybrid coach: rules pin facts, LLM phrases them.** The factual content of the review comes from `analyze` + raw match data. The LLM only synthesizes prose from supplied beats and is explicitly told not to invent events. This bounds hallucination risk while still getting human-sounding output.
- **Discovery via JOIN, fetch lazily.** `bracket-fetch` and `refresh-parses` use `/explorer` JOINs to learn ID + parse-status in batches, then only fetch full match JSONs for matches that are actually parsed. Cuts API calls from 3N to ~1N over the parse lifecycle.
- **Coach memory across sessions.** Each coach run appends to `data/coach_memory.json`; the next call injects the last 5 entries so the model can recognize recurring patterns ("you've died to Pudge 3 games in a row").
- **Secrets in `.env`, never in source.** `ANTHROPIC_API_KEY` is read from the environment at the call site, never logged, never written to disk. `.env` is gitignored.
