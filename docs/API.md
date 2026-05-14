# API Reference

All commands are invoked as:

```bash
docker compose run --rm app python -m app.cli <subcommand> [args]
```

Schema bootstrap runs on every invocation — no separate init step.

## CLI subcommands

### `account`

Print the resolved account ID from `.env`. Sanity-check that env wiring is working.

**Output:** `account_id: <int>`

---

### `profile [--refresh]`

Fetch and cache your OpenDota profile. Caches to `data/profile.json`; subsequent runs read from disk unless `--refresh` is passed.

**Args:**
- `--refresh` — bypass disk cache, force re-fetch.

**Output (JSON):**
```json
{
  "account_id": 446619601,
  "personaname": "ak3zio",
  "rank_tier": 43,
  "computed_mmr_turbo": 3848.29
}
```

**Errors:** `SystemExit` if `ACCOUNT_ID` env var is unset or OpenDota has no `rank_tier` for the account.

---

### `bracket-fetch --limit N --window W`

Discover Turbo matches at your rank bracket via a single `/explorer` JOIN that returns ID + summary + parse-status in one call (3N → ~1N optimization). Match JSONs are fetched lazily — only for matches that are *already parsed*. Unparsed matches are recorded as DB rows so `request-parses` can queue them.

**Args:**
- `--limit N` (default `500`) — how many match IDs to discover.
- `--window W` (default `10`) — rank tier window (e.g., rank 43, W=10 → 33..53).

**Output (JSON):**
```json
{
  "rank_tier": 43,
  "window": 10,
  "discovered": 500,
  "parsed_at_discovery": 73,
  "json_fetched": 73,
  "api_calls": 74
}
```

`api_calls = 1 explorer call + N_parsed JSON fetches`. Compared to the previous version (`1 + N` regardless of parse status), this saves ~N_unparsed calls per cycle.

---

### `match-fetch <match_id>`

Fetch one match by ID, cache the JSON, upsert a summary row.

**Args:**
- `match_id` (positional, int) — OpenDota match ID.

**Output (JSON):**
```json
{
  "match_id": 8810012394,
  "parsed": false,
  "duration": 1144,
  "avg_rank_tier": 41,
  "radiant_win": true
}
```

---

### `request-parses --limit N`

POST `/request/{id}` for unparsed matches in the DB, queueing them for OpenDota's parse service.

**Args:**
- `--limit N` (default `200`) — how many unparsed matches to submit.

**Output (JSON):**
```json
{
  "unparsed_in_db": 50,
  "requested": 50,
  "failed": 0
}
```

---

### `refresh-parses --limit N`

Batch-check parse status of unparsed DB matches via `/explorer` (1 call per ~200 IDs), then fetch the JSONs of any newly-parsed ones. 3N → ~1N+K, where K = newly-parsed count.

**Args:**
- `--limit N` (default `500`) — how many unparsed matches to re-check.

**Output (JSON):**
```json
{
  "unparsed_in_db": 480,
  "newly_parsed": 73,
  "api_calls": 76
}
```

`api_calls = explorer batches (ceil(N/200)) + JSON fetches for newly-parsed`. Typical workflow: `request-parses`, wait 10–30 min, `refresh-parses`, repeat.

---

### `snapshots [--rebuild]`

Extract per-minute training rows from parsed matches in the disk cache; insert into the `snapshots` table.

**Args:**
- `--rebuild` (default `false`) — re-process all parsed matches (default: only matches that don't yet have any snapshot rows).

**Output (JSON):**
```json
{
  "matches_processed": 250,
  "snapshot_rows": 5234
}
```

---

### `train [--n-estimators N]`

Train the XGBoost win-prob classifier on snapshots. Saves the model and metrics.

**Args:**
- `--n-estimators N` (default `400`) — boosting rounds (early stopping at 20 rounds without improvement).

**Output (JSON):** identical content also written to `data/model_meta.json`:
```json
{
  "n_rows": 5234,
  "n_matches": 250,
  "n_train_rows": 4187,
  "n_val_rows": 1047,
  "val_log_loss": 0.523,
  "val_auc": 0.812,
  "best_iteration": 137,
  "feature_cols": ["minute", "gold_adv", "xp_adv", "...", "r_hero_1", "...", "d_hero_5"]
}
```

Features: 9 base game-state features + 10 hero ID features (5 radiant + 5 dire, sorted within team for permutation stability).

Artifact saved to `data/turbo_winprob.json` (XGBoost native JSON format).

**Errors:** `SystemExit` if no snapshot rows in DB, or fewer than 20 distinct matches.

---

### `analyze <match_id> [--top-k K] [--min-impact x]`

Decision types extracted (v2):
- `item` — key item purchases (BKB, Aghs, Pipe, Manta, etc. from `KEY_ITEMS`)
- `death` — your hero killed by opponent
- `kill` — you killed an opponent hero
- `roshan` — Roshan killed by your team
- `smoke` — you bought Smoke of Deceit (gank initiation)
- `ward_obs` — you placed an observer ward
- `ward_sen` — you placed a sentry ward



Run the full inference pipeline for one match. Requires:
- A trained model at `data/turbo_winprob.json`.
- The match must be Turbo, parsed, and contain your `account_id` in `players[]`.

**Args:**
- `match_id` (positional, int).
- `--top-k K` (default `5`) — how many decisions in each of "leaks" and "kept doing this".
- `--min-impact x` (default `0.005`) — drop decisions whose `|Δ win-prob| < x`.

**Output (JSON):**
```json
{
  "match_id": 8810000000,
  "you": {
    "hero": "Storm Spirit",
    "slot": 3,
    "team": "radiant",
    "kda": "9/4/12",
    "result": "loss"
  },
  "duration_min": 22,
  "win_prob_curve": [0.500, 0.512, 0.498],
  "decisions": {
    "biggest_leaks": [
      {"t": "08:14", "type": "death", "impact": -0.041, "detail": "Died to Pudge"}
    ],
    "kept_doing_this": [
      {"t": "14:32", "type": "item", "impact": 0.064, "detail": "Bought BKB"}
    ]
  }
}
```

**Errors:**
- Non-Turbo match
- Match not yet parsed
- Account ID not in `players[]`
- No model at `MODEL_PATH`
- Match not found (HTTP 404 from OpenDota)

---

### `coach <match_id> [--model {haiku,sonnet,opus}] [--top-k K] [--min-impact x]`

Generate a natural-language coach review for one match via Claude. Internally runs `analyze()` for the structured findings, gathers raw match context (hero composition, patch, rank), applies heuristic narrative beats (phase grouping + recurring patterns), then asks Claude to write the review.

**Requires:**
- `ANTHROPIC_API_KEY` in `.env` (the file is gitignored — never commit a key).
- Same prerequisites as `analyze`: trained model, parsed Turbo match, you in `players[]`.

**Args:**
- `match_id` (positional, int).
- `--model {haiku,sonnet,opus}` (default `sonnet`) — selects the Claude model. Aliases map to `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-7`.
- `--top-k K` (default `6`) — passed through to `analyze`.
- `--min-impact x` (default `0.005`) — passed through to `analyze`.

**Output:** writes markdown to `data/reviews/<match_id>.md`, prints a small JSON receipt to stdout:

```json
{
  "match_id": 8810000000,
  "model": "claude-sonnet-4-6",
  "review_path": "/code/data/reviews/8810000000.md",
  "usage": {
    "input_tokens": 1842,
    "output_tokens": 612,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0
  }
}
```

**Session memory:** every coach run appends a heuristic summary (themes, hero, KDA, result, date) to `data/coach_memory.json`. The last 5 entries are injected into subsequent prompts under a "Recent match history" section, so Claude can spot recurring patterns ("you've died to Pudge 3 games running"). Delete the file to reset.

**Cost:** roughly $0.005–0.03 per call on Sonnet 4.6, depending on match complexity.

**Errors:**
- `ANTHROPIC_API_KEY` unset
- `AuthenticationError` — bad key
- `RateLimitError` — Anthropic rate limit hit
- All `analyze` errors (not parsed, not in match, etc.)

---

## Internal module API

### `app.fetcher`

| Function | Purpose |
|---|---|
| `fetch_profile(force=False) -> dict` | Profile JSON (cached on disk) |
| `your_rank_tier() -> int` | Your `rank_tier` from the cached profile |
| `fetch_match(match_id, force=False) -> dict` | Full match JSON (cached) |
| `request_parse(match_id) -> None` | POST `/request/{id}` |
| `is_parsed(match: dict) -> bool` | True if `version` set + `gold_t` arrays present |
| `avg_rank_tier(match: dict) -> int \| None` | Mean of `players[i].rank_tier` |
| `your_player(match: dict) -> dict` | `players[i]` with `account_id == you`, or `{}` |
| `bracket_match_ids(rank_tier, window, limit) -> list[int]` | Discovery via `/explorer` |
| `sync_match(match_id) -> dict` | Fetch + upsert one match |
| `sync_bracket_matches(limit, window) -> dict` | Discover + fetch a bracket batch |
| `request_parses(limit) -> dict` | Submit parses for unparsed matches in DB |
| `refresh_parses(limit) -> dict` | Re-fetch unparsed matches |
| `upsert_match(conn, match) -> None` | INSERT … ON CONFLICT UPDATE |

### `app.snapshots`

| Function | Purpose |
|---|---|
| `extract(match: dict) -> list[dict]` | Per-minute snapshot rows in memory |
| `build_all(only_missing=True) -> dict` | Process parsed matches in DB into `snapshots` table |

### `app.train`

| Function | Purpose |
|---|---|
| `train(n_estimators=400) -> dict` | Group-aware train/val, fit XGBoost, save artifact + metrics |
| `FEATURE_COLS` | List of feature column names (shared with inference) |

### `app.analyze`

| Function | Purpose |
|---|---|
| `analyze(match_id, top_k=5, min_impact=0.005) -> dict` | Full inference pipeline for one match |
| `heroes_by_id() -> dict[int, dict]` | Cached `/heroes` lookup (id → hero dict) — shared with `coach` |
| `KEY_ITEMS` | Curated mapping `npc item key → display name` |

### `app.coach`

| Function | Purpose |
|---|---|
| `coach(match_id, model="sonnet", top_k=6, min_impact=0.005) -> dict` | Run `analyze`, build heuristic beats, call Claude, write markdown |
| `MODEL_ALIASES` | `{haiku,sonnet,opus}` → exact Claude model ID |
| `SYSTEM_PROMPT` | The coach system prompt (cacheable via top-level `cache_control`) |

### `app.db`

| Function | Purpose |
|---|---|
| `connect() -> psycopg.Connection` | autocommit=True connection |
| `ensure_schema() -> None` | Idempotent schema bootstrap |
| `SCHEMA_SQL` | Authoritative schema (matches + snapshots) |

### `app.config`

| Symbol | Purpose |
|---|---|
| `ACCOUNT_ID` | `int \| None` — from env |
| `DATABASE_URL` | from env, default for local dev |
| `OPENDOTA_BASE` | `"https://api.opendota.com/api"` |
| `TURBO_GAME_MODE` | `23` |
| `DATA_DIR`, `MATCHES_DIR`, `PROFILE_PATH`, `MODEL_PATH` | paths |
| `require_account_id() -> int` | raises if unset |

## OpenDota endpoints used

| Method | Path | Used by |
|---|---|---|
| GET | `/players/{account_id}` | `profile` |
| GET | `/matches/{match_id}` | `bracket-fetch`, `match-fetch`, `refresh-parses`, `analyze` |
| GET | `/explorer?sql=...` | `bracket-fetch` (discovery) |
| GET | `/heroes` | `analyze`, `coach` (one-time, cached) |
| POST | `/request/{match_id}` | `request-parses` |

The `coach` command additionally calls Anthropic's `messages.create` endpoint via the `anthropic` SDK.

All GETs go through the retrying client in `app/fetcher.py:_get`, which honors 429 via exponential backoff.

## Exit codes

| Code | When |
|---|---|
| 0 | Success |
| 1 | argparse usage error |
| 2 | `SystemExit` from a guard: match not parsed, model missing, account not in match, match not found on OpenDota, `ANTHROPIC_API_KEY` unset, Anthropic auth/rate-limit/API error |

Errors that aren't expected (HTTP 500s, DB connection failures) bubble up as Python tracebacks with non-zero exit.
