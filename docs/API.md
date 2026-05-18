# API Reference

All CLI commands are invoked as:

```bash
docker compose run --rm app python -m app.cli <subcommand> [args]
```

Schema bootstrap runs on every invocation — no separate init step.

**Account resolution.** Subcommands that operate on a user accept `--account <id>`. If omitted, they fall back to `$ACCOUNT_ID` from `.env` (this preserves the original single-user workflow). The web UI sets the account via Steam sign-in.

## CLI subcommands

### `account [--account ID]`

Print the resolved account id. Sanity-check env / flag wiring.

**Output:** `account_id: <int>`

---

### `profile [--account ID] [--refresh]`

Fetch and cache a user's OpenDota profile. Caches to `data/profiles/<account_id>.json`.

**Args:**
- `--account ID` — account to fetch for. Defaults to `$ACCOUNT_ID`.
- `--refresh` — bypass disk cache.

**Output (JSON):**
```json
{
  "account_id": 446619601,
  "personaname": "ak3zio",
  "rank_tier": 43,
  "computed_mmr_turbo": 3848.29
}
```

**Errors:** `SystemExit` if no account id was resolvable, or OpenDota has no `rank_tier` for it.

---

### `bracket-fetch [--account ID] [--rank N] [--limit N] [--window W]`

Discover Turbo matches at a rank bracket via a single `/explorer` JOIN (ID + summary + parse-status in one call). Match JSONs are fetched lazily — only for already-parsed matches. Unparsed matches are recorded as DB rows so `request-parses` can queue them.

**Args:**
- `--account ID` — used to populate `user_matches` if the signed-in user appears in any discovered match.
- `--rank N` — target rank_tier (e.g., `15` for Herald 5). Defaults to the signed-in account's own rank. Use to bootstrap training data across multiple brackets — see `scripts/bootstrap_brackets.sh`.
- `--limit N` (default 500), `--window W` (default 10).

**Output (JSON):**
```json
{
  "rank_tier": 43, "window": 10,
  "discovered": 500, "parsed_at_discovery": 73,
  "json_fetched": 73, "api_calls": 74
}
```

---

### `match-fetch [--account ID] <match_id>`

Fetch one match, cache JSON, upsert into `matches`. If `--account` is given and that user appears in `players[]`, also writes a `user_matches` row.

---

### `request-parses [--limit N]`

POST `/request/{id}` for unparsed matches in the DB. Skips any match parse-requested within the last 24h (`matches.parse_requested_at` cooldown). **Account-agnostic** — operates on the shared `matches` table.

429s and 5xx/Cloudflare 52x retry with exponential backoff (~63s total per match).

---

### `refresh-parses [--account ID] [--limit N]`

Per-match `/matches/{id}` re-fetch for unparsed DB matches. Updates `matches.parsed` when version is now set. Ordered oldest-`parse_requested_at` first. If `--account` is given, populates `user_matches` for any rows where the user appears in `players[]`.

Why per-match rather than batched `/explorer`: OpenDota's `matches` SQL table lags actual parse state visible via `/matches/{id}` (sometimes hours). The earlier batched approach silently missed parsed matches. Per-match is slower but reliable.

---

### `snapshots [--rebuild]`

Account-agnostic. Extract per-minute training rows from parsed matches in the disk cache; insert into `snapshots`.

---

### `train [--n-estimators N]`

Account-agnostic. Train the XGBoost win-prob classifier (one shared model across all users — rank-conditioned via `avg_rank_tier`).

Saves to `data/turbo_winprob.json` + `data/model_meta.json`. Auto-syncs `docs/TRAINING.md` history.

---

### `refresh-doc`

Re-syncs `docs/TRAINING.md` from existing `data/model_meta.json`. No retrain, no API calls.

---

### `training-status`

Show how many new parsed matches have accumulated since the last `train` invocation. Used as a heuristic to know when to retrain.

**Output (JSON):**
```json
{
  "trained": true,
  "trained_at": "2026-05-15T...",
  "parsed_at_train": 997,
  "current_parsed": 1542,
  "delta": 545
}
```

Hint also emitted automatically by `refresh-parses` (in its result JSON) once the delta crosses 500.

---

### `build-baselines`

Scan every cached parsed match JSON, compute per-`(rank_bucket, hero_id, item_key)` median purchase time. Write `data/baselines.json`. Used by `coach` to add "BKB at 17:30 vs bracket median 14:00" advice. Re-run after each batch of parsed matches accumulates.

Noise floor: 5 samples per bucket-hero-item. Below that, the bucket is dropped.

**Output (JSON):**
```json
{
  "matches_scanned": 1542,
  "buckets_published": 1842,
  "buckets_dropped_below_noise_floor": 4291,
  "path": "/code/data/baselines.json"
}
```

---

### `sweep [--out PATH]`

Run a small XGBoost hyperparameter sweep (`max_depth × learning_rate × n_estimators`). Prints results; **does not save the model**. Use the winning config with `train --n-estimators N` (other params are not yet flag-exposed in `train`; edit `app/train.py` or open a focused PR).

**Args:** `--out PATH` — optional path to write the full results JSON.

---

### `analyze [--account ID] <match_id> [--top-k K] [--min-impact x]`

Full inference pipeline for one match. Requires: trained model, match is Turbo + parsed, user is in `players[]`.

**Decision types:** `item · death · kill · roshan · smoke · ward_obs · ward_sen`.

**Output (JSON):**
```json
{
  "match_id": 8810000000,
  "account_id": 446619601,
  "you": {"hero": "Storm Spirit", "slot": 3, "team": "radiant", "kda": "9/4/12", "result": "loss"},
  "duration_min": 22,
  "win_prob_curve": [0.500, 0.512, 0.498],
  "decisions": {
    "biggest_leaks":   [{"t": "08:14", "type": "death", "impact": -0.041, "detail": "Died to Pudge"}],
    "kept_doing_this": [{"t": "14:32", "type": "item",  "impact":  0.064, "detail": "Bought BKB"}]
  }
}
```

---

### `coach [--account ID] <match_id> [--model {haiku,sonnet,opus}] [--top-k K] [--min-impact x] [--stream]`

Generate a markdown coach review via Claude. Writes to `data/reviews/<account_id>/<match_id>.md`.

**Key resolution.** If the user has a BYO key saved (in the `users` table), it's used. Otherwise the server-side `ANTHROPIC_API_KEY` is used, subject to the per-user daily cap (`DAILY_COST_CAP_CENTS`).

**Output (JSON receipt):**
```json
{
  "match_id": 8810000000,
  "account_id": 446619601,
  "model": "claude-sonnet-4-6",
  "review_path": "/code/data/reviews/446619601/8810000000.md",
  "memory_entries": 3,
  "byo_key": false,
  "cost_cents": 4,
  "usage": {"input_tokens": 1842, "output_tokens": 612, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
}
```

`--stream` prints chunks to stdout as Claude generates them (useful on Opus); the JSON receipt still prints at the end.

**Errors:** all of `analyze`'s errors; `cost.BudgetExceeded` if the daily cap is hit on the server key; auth/rate-limit/API errors from Anthropic.

---

## `app.crypto` (helper)

```bash
docker compose run --rm app python -m app.crypto gen
```

Prints a fresh Fernet key. Paste it into `.env` as `FERNET_KEY=...`. Required for BYO Anthropic-key storage (the encryption-at-rest mechanism).

---

## Auth service (`app.auth`, FastAPI on :8502)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | `{"ok": true, "service": "auth"}` |
| `GET` | `/auth/steam/login` | 302 → Steam OpenID checkid_setup URL |
| `GET` | `/auth/steam/callback` | Verifies the Steam assertion, upserts `users` row, mints JWT, 302s to `WEB_PUBLIC_URL/?token=…` |

JWTs are HMAC-SHA256 signed with `JWT_SECRET`. Payload: `{"account_id": int, "exp": int}`. TTL = 7 days. Validated by `app.auth.verify_jwt()` (used by Streamlit on each page load).

---

## Internal module API

### `app.fetcher`

| Function | Purpose |
|---|---|
| `fetch_profile(account_id, force=False) -> dict` | Profile JSON (cached to `profiles/<id>.json`) |
| `rank_tier_for(account_id) -> int` | Rank from the cached profile |
| `fetch_match(match_id, force=False) -> dict` | Full match JSON (cached) |
| `player_for(match, account_id) -> dict` | `players[i]` matching that account, or `{}` |
| `is_parsed(match) -> bool` | True if `version` + `gold_t` arrays present |
| `avg_rank_tier(match) -> int \| None` | Mean of `players[i].rank_tier` |
| `request_parse(match_id) -> None` | POST `/request/{id}` |
| `sync_match(match_id, account_id=None) -> dict` | Fetch + upsert. Populates `user_matches` if `account_id` given. |
| `sync_bracket_matches(account_id, limit, window) -> dict` | Discover + fetch a bracket batch |
| `request_parses(limit) -> dict` | Submit parses, with cooldown |
| `refresh_parses(limit, account_id=None) -> dict` | Re-fetch unparsed, optionally writing `user_matches` |
| `upsert_match(conn, match, account_id=None) -> None` | INSERT…ON CONFLICT UPDATE; optionally writes `user_matches` |

### `app.analyze`

| Function | Purpose |
|---|---|
| `analyze(match_id, account_id=None, top_k=5, min_impact=0.005) -> dict` | Inference pipeline. `account_id` falls back to `$ACCOUNT_ID`. |
| `heroes_by_id() -> dict[int, dict]` | Cached `/heroes` lookup |
| `KEY_ITEMS` | `{npc_item_key: display_name}` |

### `app.coach`

| Function | Purpose |
|---|---|
| `coach(match_id, account_id=None, model="sonnet", on_chunk=None, …) -> dict` | Full coach run. Resolves key, checks budget, charges. If `on_chunk` given, streams via `messages.stream()` and calls the callback per text chunk. |
| `MODEL_ALIASES` | `{haiku,sonnet,opus}` → exact Claude model id |
| `SYSTEM_PROMPT` | Coach system prompt |

### `app.baselines`

| Function | Purpose |
|---|---|
| `build() -> dict` | Scan cached match JSONs, compute medians, write `data/baselines.json` |
| `load() -> dict` | Read `data/baselines.json` (or empty stub) |
| `lookup(baselines, rank_tier, hero_id, item_key) -> dict \| None` | Resolve a specific baseline |
| `format_delta(observed_seconds, baseline) -> str` | Format the prompt-ready "observed X vs median Y" string |

### `app.cost`

| Function | Purpose |
|---|---|
| `current_usage(account_id) -> dict` | `{daily_cents, monthly_cents, cap_cents}` (resets daily counter past UTC midnight) |
| `check_budget(account_id, use_byo_key) -> None` | Raises `BudgetExceeded` if over cap (server key only) |
| `charge(account_id, cents, use_byo_key) -> None` | Records a charge — daily counter advances only for server-key calls |
| `estimate_cents(model_id, usage) -> int` | Convert Anthropic `usage` dict to cents |

### `app.crypto`

| Function | Purpose |
|---|---|
| `encrypt_key(plaintext) -> str` | Fernet-encrypt a BYO API key |
| `decrypt_key(ciphertext) -> str` | Decrypt at use time |
| `generate_key() -> str` | New Fernet key (for `.env`) |

### `app.auth`

| Function | Purpose |
|---|---|
| `make_jwt(account_id) -> str` | Mint a signed token |
| `verify_jwt(token) -> dict \| None` | Decode & validate; `None` on failure |

### `app.db`

| Function | Purpose |
|---|---|
| `connect() -> psycopg.Connection` | autocommit=True |
| `ensure_schema() -> None` | Idempotent bootstrap of all tables (matches, snapshots, users, user_matches) |
| `SCHEMA_SQL` | Authoritative schema |

### `app.config`

| Symbol | Purpose |
|---|---|
| `ACCOUNT_ID` | `int \| None` — single-user CLI fallback |
| `DATABASE_URL`, `DATA_DIR`, `MATCHES_DIR`, `MODEL_PATH` | paths / connection |
| `PROFILES_DIR`, `MEMORY_DIR`, `REVIEWS_DIR` | per-user roots |
| `profile_path(aid)`, `memory_path(aid)`, `reviews_dir_for(aid)` | per-user path helpers |
| `JWT_SECRET`, `FERNET_KEY` | auth + crypto |
| `DAILY_COST_CAP_CENTS` | server-key cap (default 25¢/day) |
| `STEAM_OPENID_REALM`, `AUTH_PUBLIC_URL`, `WEB_PUBLIC_URL` | Steam OpenID + redirects |
| `ANTHROPIC_API_KEY` | server-side default key |
| `OPENDOTA_API_KEY` | Premium tier key; auto-injected as `?api_key=…` on every OpenDota call when set. Empty → free tier. |
| `resolve_account_id(explicit=None) -> int` | flag-or-env resolver used by CLI + module APIs |
| `require_account_id() -> int` | back-compat alias for the env-only resolver |

## OpenDota endpoints used

| Method | Path | Used by |
|---|---|---|
| GET | `/players/{account_id}` | `profile` |
| GET | `/matches/{match_id}` | `bracket-fetch`, `match-fetch`, `refresh-parses`, `analyze`, `coach` |
| GET | `/explorer?sql=...` | `bracket-fetch` (discovery) |
| GET | `/heroes` | `analyze`, `coach` (one-time, cached) |
| POST | `/request/{match_id}` | `request-parses` |

`coach` additionally calls Anthropic `messages.create`. `auth` calls Steam OpenID `https://steamcommunity.com/openid/login` for the `check_authentication` verify.

All GETs go through `app.fetcher._get`'s retrying client (429 + 5xx exponential backoff).

## Exit codes

| Code | When |
|---|---|
| 0 | Success |
| 1 | argparse usage error |
| 2 | `SystemExit` from a guard: no account resolved, match not parsed/Turbo/in your games, model missing, match not found, no Anthropic key + no BYO, budget exhausted, auth/rate-limit error |
