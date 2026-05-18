# Troubleshooting

Operational gotchas collected from running this pipeline end-to-end. Roughly grouped by where the problem surfaces.

## OpenDota

### `/explorer` returns stale parse status

**Symptom:** `refresh-parses` reports rows as unparsed even though `/matches/{id}` shows them parsed (gold_t arrays populated, `version` set).

**Cause:** OpenDota's `matches` SQL table on `/explorer` is materialized from a separate pipeline that can lag the public match endpoint by hours.

**Fix:** Already in the code — `refresh-parses` calls `/matches/{id}` per match rather than batching via `/explorer`. Don't switch back to the batched approach.

### Cloudflare 522 / 524 on `/explorer`

**Symptom:** `_explorer chunk N/M failed (524 Server Error)` warnings.

**Cause:** OpenDota fronted by Cloudflare; long-running SQL queries time out at the CF edge.

**Fix:** `fetcher._RETRYABLE_STATUSES` already includes 520-524; backoff retry handles it. If it persists, reduce the chunk size in `_explorer_parse_status` (currently 50) or drop to a single ID per call.

### 429 mid-batch

**Symptom:** request rate >60/min triggers HTTP 429.

**Fix:** Both `_get` and `_post` retry with exponential backoff (1,2,4,8,16,32s). One 429 inside a batch loses ~1 minute; not worth aborting the batch. If you're routinely seeing many 429s, lower `--limit` or get an OpenDota Premium key (5× rate limit).

### A match stays unparsed for >24h

**Causes (in rough order):**
1. Replay older than ~14 days — OpenDota can't parse expired replays. **Mitigation:** `bracket-fetch` sorts by `start_time DESC`.
2. Match started behind a private lobby (`lobby_type` ≠ 0,7). We already filter to public matchmaking.
3. OpenDota parse queue is genuinely backed up (peak hours). Re-run `request-parses` after 24h — the cooldown will then re-submit it.

## Database

### `relation "users" does not exist`

**Cause:** Schema bootstrap didn't run (you're hitting the DB outside the CLI, e.g., from a script that doesn't call `db.ensure_schema()`).

**Fix:** Any CLI invocation triggers the bootstrap. Simplest reset: `docker compose run --rm app python -m app.cli account` (just resolves your account; side-effect ensures schema).

### Foreign-key violation when inserting `user_matches`

**Cause:** Inserting a `user_matches` row before its `users` row exists.

**Fix:** `fetcher._upsert_user_match` upserts a stub `users` row first. If you're writing custom SQL, do the same: `INSERT INTO users (account_id) VALUES (...) ON CONFLICT DO NOTHING` before the user_matches insert.

### Postgres takes a long time to start (first run)

Healthcheck retries every 5s up to 10 times. If the volume is fresh, initial `initdb` takes 10–30s. The `auth` and `web` services wait via `depends_on: service_healthy` so they won't crash; they'll just stall briefly.

## Auth / Steam OpenID

### Steam OpenID redirect lands on localhost but Streamlit doesn't recognize me

**Symptom:** You click "Sign in with Steam", complete the Steam flow, land back at `http://localhost:8501/?token=...` but the page shows "Please sign in" still.

**Possible causes:**
1. `JWT_SECRET` differs between the `auth` and `web` containers. Both read from the same `.env`, but if you set the env variable inline for only one service the other can't verify the token. **Fix:** set in `.env`, not at the docker-compose `environment:` level.
2. `JWT_SECRET` was rotated since the token was minted. Sign in again.
3. Token TTL (7 days) elapsed. Sign in again.

**Diagnose:** copy the token from the URL, then in a Python shell inside the `web` container:

```python
from app import auth
auth.verify_jwt("<paste>")  # → None means rejection
```

### Steam callback returns "Steam OpenID verification failed"

**Cause:** The mode=check_authentication POST to `steamcommunity.com/openid/login` came back with `is_valid:false`. Almost always means the assertion is replayed or the realm doesn't match.

**Fix:** Confirm `STEAM_OPENID_REALM` in `.env` is a prefix of `AUTH_PUBLIC_URL/auth/steam/callback`. For local dev both should be `http://localhost:8502`.

### Steam OpenID won't complete on pure localhost

**Cause:** Steam's verification call goes from Steam's servers to your auth service. Steam can't reach `localhost:8502` from the public internet.

**Fix:** For local end-to-end Steam testing, use ngrok or a similar tunnel and set `AUTH_PUBLIC_URL` + `STEAM_OPENID_REALM` to the tunnel URL. **OR** use the dev-env fallback: with `ACCOUNT_ID` set in `.env` the web app auto-logs you in without going through Steam.

## Anthropic / Coach

### `BudgetExceeded` on the first coach run today

**Cause:** Daily cap `DAILY_COST_CAP_CENTS` was set very low (e.g., 1¢).

**Fix:** Raise the cap in `.env`, restart `web`, OR paste your own Anthropic key in Settings (BYO bypasses the cap).

### `AuthenticationError` from Anthropic when you JUST pasted a valid key

**Possible causes:**
1. Key copy/paste truncation — Anthropic keys are long; check the trailing portion.
2. Key is disabled in your Anthropic console.
3. Wrong key type — must start with `sk-ant-`.

The Settings page validates via `client.models.list()` (a free call) before saving, so a bad key won't be persisted in the first place.

### "Stored key cannot be decrypted with current FERNET_KEY"

**Cause:** You rotated `FERNET_KEY` in `.env` since the BYO key was saved.

**Fix:** Remove the stored key (Settings → Remove my BYO key) and paste it again; it'll re-encrypt with the new Fernet key.

### Streaming output stops mid-review

**Possible causes:**
1. Connection drop — the `with client.messages.stream(...)` block will raise. The CLI prints a partial review and exits non-zero.
2. `max_tokens` cap hit — review truncated mid-sentence. Bump `max_tokens` in `coach.coach` (currently 3500).
3. Anthropic API outage. Retry.

## Model / training

### `analyze` complains about feature count mismatch

**Symptom:** `XGBoostError: feature_names mismatch` or similar.

**Cause:** You upgraded the codebase (which added new entries to `FEATURE_COLS`) without retraining. The on-disk model was fit with the old feature set.

**Fix:** `analyze._load_model` already trims `FEATURE_COLS` to the model's `n_features_in_`, so existing models keep working. If you still see this, the trim isn't activating — make sure your installed `xgboost` is recent enough to populate `n_features_in_`.

To activate new features: `docker compose run --rm app python -m app.cli snapshots --rebuild && python -m app.cli train`.

### `val_auc` regressed after retraining

**Possible causes:**
1. Patch drift — new matches were collected on a different patch with different balance. Retrain on a per-patch slice if it's bad enough.
2. Data corruption (rare) — check `model_meta.json:n_rows` is plausible.
3. Hyperparameters drifted from optimal. Run `docker compose run --rm app python -m app.cli sweep` to re-check the grid.

### Per-bracket calibrator silently skipped

**Symptom:** `analyze` output has `"calibrated": false` even though calibrators are present.

**Cause:** The match's `avg_rank_tier // 10` doesn't match any fitted bracket. Fitting requires ≥50 val-fold samples in that bracket; brackets with too few are skipped.

**Fix:** Accumulate more matches in that bracket, retrain.

## Streamlit / web UI

### Pages don't appear in the sidebar

**Cause:** Streamlit autodiscovers `pages/` only next to the entry script. The entry must be `app/web.py` (or whatever you set in compose's `streamlit run` command), and `pages/` must live at `app/pages/`. The repo is wired this way; if you renamed things, restore the layout.

### Sidebar shows `Account 0` after sign-in

**Cause:** JWT decoded but `account_id` was 0 (i.e., your Steam ID was exactly `76561197960265728`). That's the conversion-from-zero edge case and means a real auth bug. File a debug.

### "missing ScriptRunContext" warnings when running CLI

These are harmless — Streamlit's session-state module logs them when imported outside a Streamlit run. They appear because `app.web_auth` imports `streamlit`. Ignore them.

## Smoke / CI

### `scripts/smoke.sh` reports a failure but pytest passes

The smoke test exercises the full Docker/networking stack (web, auth, db containers + curl). pytest only tests pure Python. A passing pytest + failing smoke usually means container wiring or env vars rather than code.

### `auth service responds on :8502/healthz` smoke fails

The auth service can start with empty `JWT_SECRET` (healthz doesn't require it). If the healthz curl fails entirely, the auth container probably crashed at import time — check `docker compose logs auth`.

## Bootstrapping all 7 brackets (Path C refetch)

### `scripts/wipe_data.py` refuses to run

By design — it requires `--confirm` AND an interactive prompt. Default mode expects `wipe`; `--full` mode expects `wipe everything` (more emphatic phrase, harder to mistype). To run non-interactively (CI / scripted), pipe the phrase into stdin:

```bash
echo wipe | docker compose run --rm -T app python scripts/wipe_data.py --confirm
echo "wipe everything" | docker compose run --rm -T app python scripts/wipe_data.py --confirm --full
```

### What `--full` actually drops

In addition to the default-mode drops (matches/snapshots/user_matches tables, model artifacts, match JSONs):

- `users` table rows — Steam IDs, BYO Anthropic keys, cost counters all gone
- `data/profiles/*.json` — per-user OpenDota profile cache
- `data/coach_memory/*.json` — per-user coach memory
- `data/reviews/<account_id>/*` — every user's review markdown directory
- `data/heroes.json` — gets re-fetched on next analyze

What stays: `.env`, the Postgres schema (re-bootstrapped on next CLI run), the codebase. After `--full` you can sign in via Steam OpenID and you'll be a brand-new user with zero history.

### `wipe_data.py` succeeded but didn't drop the match JSONs

Default behavior drops them. If you passed `--keep-matches-json`, that's why. Re-run without the flag to drop them, or `rm -rf data/matches/*.json` manually.

### Higher brackets (Ancient, Divine) won't reach target

This is **expected**, not a bug. OpenDota's `public_matches` table thins out at high MMR because:

1. **Population pyramid** — Divine is ~2.5% of the player base; Ancient ~5%. Far fewer matches at those ranks are played per day, so the `public_matches` firehose has correspondingly fewer rows there.
2. **Replay expiry** — OpenDota can only parse replays from the last ~14 days. Older Divine matches have unrecoverable replays, even if discoverable.
3. **Fair-queue parsing** — the parse queue doesn't prioritize by rank, so without `--rank-min/--rank-max` targeting, high-rank requests sit behind 100× more low-rank requests.

`scripts/collect_data.sh` handles this via:
- **Per-bracket targeted `request-parses`** (uses the new `--rank-min`/`--rank-max` flags) so Divine doesn't starve.
- **Stuck-detection**: a bracket that makes no progress for `STUCK_CYCLES` consecutive cycles is marked STALLED and stops blocking script exit.

Expected end-state with target=400: Herald/Guardian/Crusader/Archon often exceed target; Legend reaches it; Ancient lands at 150–300; Divine lands at 50–200. The model still works — `_fit_calibrators` skips brackets below the 50-val-sample threshold and `build-baselines` skips (rank × hero × item) tuples below the 5-sample noise floor.

### Collection stopped mid-cycle (Ctrl-C, machine reboot)

Resume by re-running `scripts/collect_data.sh`. Every step is idempotent: discovery upserts, the 24h `parse_requested_at` cooldown skips duplicate POSTs, refresh-parses picks up where it left off. Status arrays reset (no persistence) but the underlying DB state ensures correctness.

### Want to top up a single bracket later

After `train_model.sh` runs, you can extend any single bracket without re-collecting everything:

```bash
docker compose run --rm app python -m app.cli bracket-fetch --rank 75 --window 7 --limit 2000
docker compose run --rm app python -m app.cli request-parses --rank-min 70 --rank-max 79 --limit 500
# wait for parses to complete (hours-days depending on Premium)
docker compose run --rm app python -m app.cli refresh-parses --limit 500
# then re-run train_model.sh to incorporate the new data
bash scripts/train_model.sh
```

### After bootstrap, val_auc dropped vs single-bracket model

Expected and acceptable. Single-bracket models overfit to one regime; multi-bracket models trade peak-bracket AUC for cross-bracket reliability. Calibrated win-probs across all 7 brackets are more useful than 0.85 AUC in one. If the drop is more than ~0.03, run `dc sweep` to verify the hyperparameters still suit the broader data.

### OpenDota Premium quota burned through

Premium tier is pay-per-call (~$0.0001/call) with no daily cap, but if you're noticing unexpected charges, check for runaway processes: `docker compose ps` will show running containers. `ps aux | grep collect_data` to find lingering bash scripts. Kill any unintended ones with Ctrl-C in their terminal or `kill <pid>`.
