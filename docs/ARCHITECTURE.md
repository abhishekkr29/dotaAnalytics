# Architecture

## High-level diagram

```
┌────────────────────────────────────────────────────────────────────────┐
│  docker compose                                                        │
│                                                                        │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐               │
│   │  web  (8501) │   │ auth  (8502) │   │  app  (CLI)  │               │
│   │  Streamlit   │   │  FastAPI     │   │  argparse    │               │
│   │  multi-page  │   │  Steam OIDC  │   │  one-shot    │               │
│   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘               │
│          │  JWT             │                   │                      │
│          └─────────┬────────┘                   │                      │
│                    ▼                            ▼                      │
│             ┌──────────────────────────────────────────┐               │
│             │             db  (postgres 16)            │               │
│             │  matches · snapshots · users · u_matches │               │
│             └──────────────────────────────────────────┘               │
│                                                                        │
│                       shared volume                                    │
│                            ▼                                           │
│   ┌──────────────────────────────────────────────────────────┐         │
│   │  ./data                                                  │         │
│   │  · matches/<match_id>.json    raw match JSON cache       │         │
│   │  · profiles/<account_id>.json per-user profile cache     │         │
│   │  · coach_memory/<aid>.json    per-user coach memory      │         │
│   │  · reviews/<aid>/<mid>.md     per-user markdown reviews  │         │
│   │  · heroes.json                hero metadata              │         │
│   │  · turbo_winprob.json         shared model               │         │
│   │  · model_meta.json            training metrics           │         │
│   └──────────────────────────────────────────────────────────┘         │
└────────────────────────────────────────────────────────────────────────┘
       │                              │
       ▼                              ▼
┌────────────────┐            ┌────────────────────┐
│  OpenDota API  │            │  Anthropic API     │
│  (free tier)   │            │  (BYO or server)   │
└────────────────┘            └────────────────────┘
              ▲
              │ Steam OpenID 2.0 (from `auth`)
       ┌──────────────┐
       │  Steam       │
       │  Community   │
       └──────────────┘
```

Four services (db / app / auth / web), one shared volume, three external dependencies (OpenDota, Anthropic, Steam OpenID).

## Module breakdown

| Module | Purpose |
|---|---|
| `app/config.py` | Reads env (`ACCOUNT_ID`, DB URL, JWT/Fernet secrets, OpenID URLs, cost cap). Per-user path helpers. `resolve_account_id(explicit)`. |
| `app/db.py` | Postgres helper + idempotent `ensure_schema()` for all four tables. |
| `app/fetcher.py` | OpenDota HTTP client with 429/5xx retry. All public functions take explicit `account_id`. `upsert_match` populates `user_matches` when given an account. |
| `app/snapshots.py` | Pure transform: parsed match JSON → per-minute snapshot dicts. |
| `app/train.py` | Group-aware train/val split, fits XGBoost. One model shared across users (rank-conditioned). Post-fit: per-bracket isotonic calibration (`data/calibrators.joblib`). Tracks `parsed_match_count_at_train` in `model_meta.json` so the auto-retrain hint can compute deltas. |
| `app/analyze.py` | `analyze(match_id, account_id, …)` — win-prob curve + scored decisions. Decision clustering for same-fight deaths. Buyback events. Replay deep links. Trims feature list to `model.n_features_in_` for forward-compat. Applies per-bracket calibration when present. **Causal-attribution filter** drops semantic impossibilities (death with positive Δwp, kill with negative Δwp) and demotes action events (items/wards/smokes/buybacks) that fall within 90s of an outcome event (death/kill) — they're co-events, not causes. |
| `app/coach.py` | Three Claude-backed surfaces: **`coach()`** (full markdown review, streaming, cached at `data/reviews/<aid>/<mid>.md`), **`recommend_per_leak()`** (Haiku, 1-2 sentence tactical advice per leak using only pre-leak causal context; cached at `data/recommendations/<aid>/<mid>.json`), **`assign_blame()`** (Stanley Parable narrator picks one player on the losing team via role-aware composite blame score, 30-50 word zinger; cached at `data/blame/<aid>/<mid>__<slot>.json`). All three gated by `cost.check_budget` → `cost.charge`. Resolve BYO vs server key. |
| `app/baselines.py` | Builds and reads per-(rank_bucket, hero, item) median purchase-time table. `data/baselines.json`. Powers the coach's "BKB at 17:30 vs bracket median 14:00" advice. |
| `app/cost.py` | Daily/monthly per-user spend tracking; `BudgetExceeded` when over cap on the server key. |
| `app/crypto.py` | Fernet encryption helpers for BYO Anthropic keys at rest. |
| `app/auth.py` | FastAPI on `:8502`. Steam OpenID 2.0 sign-in → mints JWT → 302s to web. |
| `app/web.py` | Streamlit entry / home page. Handles `?token=` handoff, dev-env fallback. |
| `app/web_auth.py` | Streamlit-side `current_user()`, `require_login()`, and the shared sidebar (cost dashboard + model picker + sign-out). |
| `app/pages/*.py` | Analyzer · History · Patterns · Reviews · Settings. Each page calls `require_login()`. |
| `app/cli.py` | argparse with `--account` parent. Calls `db.ensure_schema()` on startup. |

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
| `item` — key item bought | `players[you].purchase_log` | `key ∈ KEY_ITEMS` in `app/analyze.py` |
| `death` | opposing players' `kills_log[].key == npc_dota_hero_<yours>` | all entries |
| `kill` | `players[you].kills_log` | all entries |
| `roshan` | `objectives[type == CHAT_MESSAGE_ROSHAN_KILL]` | only credited to your team |
| `smoke` (gank init) | `players[you].purchase_log` with `key == "smoke_of_deceit"` | all entries |
| `ward_obs` | `players[you].obs_log` | all entries |
| `ward_sen` | `players[you].sen_log` | all entries |

After scoring, decisions split by sign: positive Δ → `kept_doing_this`, negative Δ → `biggest_leaks`. The two lists are disjoint; empty `biggest_leaks` in a clean win means there were no negative-Δ events above the impact threshold.

After scoring, decisions with `|impact| < min_impact` (default `0.005`) are dropped as noise. The rest are sorted by signed impact: top-K positive → "kept doing this", bottom-K → "biggest leaks".

The window is asymmetric (30s before, 90s after) because Dota decisions usually pay off in the next ~60s — a BKB bought now matters in the next fight, not the previous one.

## Database schema

Single source: `app/db.py:SCHEMA_SQL`. Runs idempotently on every CLI/service startup.

### `users`

| column | type | notes |
|---|---|---|
| `account_id` | BIGINT PK | 32-bit Dota account id (= Steam ID 64 − 76561197960265728) |
| `steam_id_64` | BIGINT UNIQUE | from Steam OpenID assertion |
| `friend_code` | TEXT | optional display value |
| `rank_tier` | INTEGER | last-known rank |
| `profile_json` | JSONB | cached OpenDota profile blob |
| `anthropic_key_encrypted` | TEXT | Fernet ciphertext, NULL if user uses the server key |
| `daily_cost_used_cents` | INTEGER | server-key spend today (resets daily) |
| `daily_cost_reset_at` | TIMESTAMPTZ | last reset wall-clock |
| `monthly_cost_used_cents` | INTEGER | running total (server + BYO) |
| `created_at`, `last_seen_at` | TIMESTAMPTZ | bookkeeping |

Indexes: `steam_id_64`.

### `user_matches`

| column | type | notes |
|---|---|---|
| `(user_id, match_id)` | BIGINT + BIGINT PK | composite |
| `slot`, `hero_id`, `kills`, `deaths`, `assists` | per-user-in-match | replaces the old `matches.your_*` columns |

Indexes: `user_id`. FKs to both `users(account_id)` and `matches(match_id)` with cascade delete.

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

- **OpenDota match JSON** → `data/matches/{match_id}.json`. Disk-first; only hits the API on miss or `force=True`.
- **Profile** → `data/profiles/{account_id}.json` (per-user; 1 call per refresh).
- **Heroes list** → `data/heroes.json` (1 call ever).
- **Model artifact** → `data/turbo_winprob.json` (XGBoost native JSON; one model shared across all users).
- **Training metrics** → `data/model_meta.json`.
- **Coach reviews** → `data/reviews/{account_id}/{match_id}.md` (per-user).
- **Coach session memory** → `data/coach_memory/{account_id}.json` (per-user; last 20 reviewed matches, last 5 injected into the next prompt).


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

## Auth pipeline (Steam OpenID → JWT)

```
   browser                 auth (:8502)              steamcommunity.com           web (:8501)
      │                        │                            │                          │
      │ GET /auth/steam/login  │                            │                          │
      │───────────────────────►│                            │                          │
      │  302 to Steam OIDC     │                            │                          │
      │◄───────────────────────│                            │                          │
      │  GET checkid_setup     │                            │                          │
      │────────────────────────────────────────────────────►│                          │
      │  user authenticates / approves                      │                          │
      │  302 back to /auth/steam/callback?openid.identity=… │                          │
      │◄────────────────────────────────────────────────────│                          │
      │ GET /auth/steam/callback                            │                          │
      │───────────────────────►│                            │                          │
      │                        │ POST check_authentication  │                          │
      │                        │───────────────────────────►│                          │
      │                        │  "is_valid:true"           │                          │
      │                        │◄───────────────────────────│                          │
      │                        │ INSERT/UPDATE users        │                          │
      │                        │ mint JWT (HS256, 7d exp)   │                          │
      │ 302 to /?token=<jwt>   │                            │                          │
      │◄───────────────────────│                            │                          │
      │ GET /?token=<jwt>      │                            │                          │
      │───────────────────────────────────────────────────────────────────────────────►│
      │                        │                            │   verify_jwt, set        │
      │                        │                            │   session_state.account  │
      │                        │                            │   clear ?token from URL  │
```

JWT-only sessions: there's no cookie store, no refresh tokens, no revocation list. To force-logout everyone, rotate `JWT_SECRET` in `.env`.

## Cost gating

`app.cost` tracks per-user Anthropic spend. Each successful `coach` run:

1. `cost.check_budget(account_id, use_byo_key)` — raises `BudgetExceeded` if the daily cap is hit on the server key. BYO bypasses.
2. Coach calls Anthropic.
3. `cost.estimate_cents(model_id, usage)` converts the response's `usage` dict into cents using `app.cost.PRICE_PER_MTOK`.
4. `cost.charge(account_id, cents, use_byo_key)` advances `daily_cost_used_cents` (only when *not* BYO) and always advances `monthly_cost_used_cents` for stats.

The daily counter resets when its `daily_cost_reset_at::date < NOW()::date` (UTC), evaluated inside the upsert.

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
- **Secrets in `.env`, never in source.** `ANTHROPIC_API_KEY`, `JWT_SECRET`, and `FERNET_KEY` all come from the environment. `.env` is gitignored. `docs/SECURITY.md` documents the full threat model.
- **Steam OpenID over passwords.** The user never types a password into our app. Steam handles authentication; we only receive a signed Steam ID. Conversion to Dota `account_id` is the same offset used everywhere in the Dota / OpenDota ecosystem.
- **One shared, rank-conditioned model across all users.** The win-prob model treats `avg_rank_tier` as a feature, so the same XGBoost classifier works for any user's bracket. Adding users doesn't require retraining; data they bring in just feeds the next training pass.
- **BYO Anthropic key as the cost-control escape hatch.** Server-side default with a daily cap keeps casual use cheap for the operator; users who want unlimited coaching paste their own key (encrypted at rest with Fernet) and pay Anthropic directly.
