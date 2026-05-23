# Planning

## Goals

A personal-scope Dota 2 Turbo decision analyzer that:

1. Trains a Turbo-specific win-probability model conditioned on player rank bracket.
2. Given any match ID, ranks the in-game decisions of a target player (you) by their measured impact on win probability.
3. Surfaces "kept doing this" (positive) and "biggest leaks" (negative) decisions in a human-readable report.
4. Synthesizes the structured findings into a natural-language coach review via Claude — saved as markdown per match.

## Out of scope

- Other game modes — only `game_mode = 23` / Turbo.
- ~~Multi-user / SaaS deployment.~~ **Phase 5a/5b done** — Steam OpenID multi-user is live, self-hosted via docker compose. A hosted/SaaS deploy is not planned.
- Real-time / live analysis. Match must be parsed first (post-game only).
- Per-position role coaching. Decisions are scored regardless of role.
- Ability usage, ward placements, smoke calls, pull/stack timing. v1 only covers item buys / deaths / kills / Roshan.

## Tier choice (Tier 1)

| Tier | What | Effort | Cost | Why not chosen |
|---|---|---|---|---|
| **Tier 1** *(chosen)* | Heuristic decision extraction + ML win-prob scorer (XGBoost) | ~1 month, weekends | $0 | — |
| Tier 2 | Sequence model (LSTM/Transformer) over event timeline | 1–2 months FT | $100–500 | Marginal accuracy gain not worth the complexity for personal use |
| Tier 3 | Counterfactual policy model (RL-style) | 3–6 months FT | $500–2,000+ | Research-project scope |

Tier 1's hybrid lets us ship something explainable and cheap that captures most of the signal of pure ML.

## Current capabilities (as of 2026-05-15)

End-to-end validated on real user matches. The CLI surface (`docker compose run --rm app python -m app.cli …`):

| Command | What it does |
|---|---|
| `account` | Sanity-check env wiring |
| `profile [--refresh]` | Fetch + cache your OpenDota profile + rank tier |
| `bracket-fetch --limit N --window W` | Discover bracket Turbo matches via single `/explorer` JOIN; fetch JSONs only for matches already parsed |
| `match-fetch <match_id>` | Fetch any single match, cache + upsert |
| `request-parses --limit N` | Queue parse requests with 24h cooldown to avoid duplicates |
| `refresh-parses --limit N` | Per-match `/matches/{id}` re-fetch (reliable; `/explorer`-based status lookup turned out to be unreliable) with 429/5xx retry |
| `snapshots [--rebuild]` | Extract per-minute training rows from cached JSONs |
| `train [--n-estimators N]` | Fit XGBoost win-prob model. Auto-updates `docs/TRAINING.md` history. |
| `refresh-doc` | Manually re-sync `docs/TRAINING.md` from `data/model_meta.json` (no retrain) |
| `analyze <match_id>` | Ranked decisions + win-prob curve JSON. 7 decision types: item, death, kill, roshan, smoke, ward_obs, ward_sen |
| `coach <match_id> [--model {haiku,sonnet,opus}]` | Markdown coach review via Claude. Auto-includes farm snapshots, item-build prescription, counterfactuals, cross-match memory |

**What the system delivers:**

- A trained, bracket-conditioned XGBoost win-probability model (`val_auc 0.854` on 997 matches; 19 features incl. 10 hero IDs).
- Per-match analyze output: full per-minute win-prob curve + ranked decisions scored by Δ win-prob.
- Per-match coach review: ~700-word markdown with phase-by-phase narrative, item-build counters, farm-pattern critique, timing-window counterfactuals, three actionable takeaways. Spots recurring patterns across reviewed matches via `data/coach_memory.json`.
- Idempotent + resumable pipeline. Failure recovery costs ~0 API calls.
- Smoke test (`scripts/smoke.sh`) — 13 checks, runs in ~30 s.

## Phase plan

| Phase | Status | Scope |
|---|---|---|
| **1. Ingest** | done | docker-compose, postgres schema, OpenDota fetcher (bracket + by-match-id) with disk + DB cache. 3N→1N discovery optimization, parse-request cooldown, 429/5xx retry on GET and POST. |
| **2. Training pipeline** | **done — validated 2026-05-15** | XGBoost win-prob model. 997 parsed matches, `val_auc 0.854`. Hero IDs as features. Saturated at current dataset size. Auto-updates `docs/TRAINING.md`. Live metrics: [TRAINING.md](TRAINING.md). |
| **3. Decision scorer** | **done — validated on 2 user matches** | `analyze` produces signed Δ-win-prob per decision. Validated against the two parsed matches in your recent history (win + loss); win curves and decision attribution track ground-truth outcomes. |
| **3b. Coach (LLM review + session memory)** | **done — validated on 2 user matches** | Hybrid: rules pin facts → Claude phrases. Full per-player profiles (KDA / GPM / NW / lane / items with timings / farm snapshots), smoke timeline, win-prob curve, kill timeline. Output includes item-build prescription + farm critique + concrete counterfactuals. Cross-match pattern detection via `coach_memory.json`. Cost ~$0.03–0.05 per review on Sonnet 4.6. |
| **4. UI (MVP)** | **done 2026-05-15** | Streamlit single-page app on `:8501` as a separate `web` service in docker-compose. Match-ID paste → auto-fetch → win-prob chart + leaks/kept decision cards → "Generate coach review" button → markdown render. Sidebar: DB status, val_auc, memory size, model-tier selector. CLI unaffected. |
| **5a. Multi-user foundation** | **done 2026-05-17** | DB: `users` + `user_matches` tables; per-user file layout (`profiles/`, `coach_memory/`, `reviews/<aid>/`). All module APIs thread `account_id`. CLI `--account` flag is required everywhere (no env fallback). FastAPI `auth` service on `:8502` with Steam OpenID 2.0 → JWT handoff. Fernet encryption for BYO Anthropic keys. Cost tracking with daily cap (`DAILY_COST_CAP_CENTS`). Migration script for the single-user → multi-user move. |
| **5b. UI multi-page + onboarding + cost dashboard** | **done 2026-05-17** | Streamlit split into `pages/`: Analyzer · History · Patterns · Reviews · Settings. Sign-in page on `/` handles Steam JWT handoff — web sign-in is always required. Sidebar component: account chip, daily/monthly spend, model picker, sign-out. Per-user onboarding card on `/` when no matches cached. Settings page: BYO key paste-validate-encrypt-store, BYO removal, usage stats. |
| **5c. Analyzer improvements** | **done 2026-05-17** | Counterfactual baselines (per-rank-bucket × hero × item median timings, `data/baselines.json` via `build-baselines` CLI) · lane/role beat passed through analyze output · decision clustering (deaths within 30s merged into one team-fight entry) · buyback events as decisions · replay deep-links (`dota2://matchid&matchtime=`) per decision. Skipped: ability-level decisions, vision impact (coord analysis), Δwp confidence intervals. |
| **5d. Coach improvements** | **done 2026-05-17** | Per-hero memory retrieval (separate prompt section for current-hero history) · per-matchup memory (cross-match recurring-enemy flags) · patch tagging on memory entries with auto-decay across patches · streaming output via `messages.stream()` (CLI `--stream` flag + Streamlit live-update). Skipped for now: semantic embeddings, pruning-by-relevance. |
| **5e. Model improvements** | **done 2026-05-17** | Per-bracket isotonic calibration (`data/calibrators.joblib`, fitted post-train on val-fold predictions per rank bucket, skip <50 samples) · backward-compat in `analyze._load_model` (trims `FEATURE_COLS` to `n_features_in_`) · auto-retrain hint via `training-status` CLI + emitted by `refresh-parses` when ≥500 new parsed since last train · hyperparam `sweep` script over `max_depth × lr × n_estimators`. Background data accumulation continues via `scripts/collect_data.sh`. New snapshot features deferred (would require `snapshots --rebuild`). |
| **5f. Validation + ops** | **done 2026-05-17** | pytest skeleton (30 tests across crypto / cost / auth / analyze / baselines / snapshots) · `docs/TROUBLESHOOTING.md` covering OpenDota / DB / Steam OpenID / Anthropic / model / Streamlit / smoke / migration warts · smoke test extended to 24 checks. |

## Validation plan — **done**

Checklist progress as of 2026-05-15:

| Step | Status |
|---|---|
| 1. Enable *Expose Public Match Data* in Dota client | ✓ done (OpenDota now returns your matches via `/recentMatches`) |
| 2. Play a few Turbo games | ✓ ~20 in recent history, 2 already parsed |
| 3. OpenDota ingestion | ✓ working |
| 4. Pipeline run (`bracket-fetch` → `request-parses` → `refresh-parses` → `snapshots` → `train`) | ✓ originally via train_loop.sh (since superseded by `collect_data.sh` + `train_model.sh`); 997 parsed matches accumulated then refactored 2026-05-18 |
| 5. `val_auc ≥ 0.78` | ✓ achieved 0.854 |
| 6. `analyze` on played matches | ✓ ran on `8807224804` (win) and `8808440501` (loss) |
| 7. Decisions match recall | ✓ win-prob curves and decision attribution tracked actual game outcomes |
| 8. `ANTHROPIC_API_KEY` set + `coach` on same matches | ✓ both reviews generated; counterfactuals, item prescriptions, and recurring-pattern detection (`repeat-deaths:Rubick` flagged) all functional |
| 9. Tune knobs as needed | ✓ several iterations: leak/kept sign-split, enriched prompt with farm + items + counterfactuals, cooldown on parse requests, retry on 429/5xx, smaller explorer chunks |

Phase 4 (UI) remains deferred — CLI is sufficient and the validation effort confirmed the CLI surface is the natural unit of work.

## Cost analysis

| Item | Cost |
|---|---|
| OpenDota API | $0 — free tier (~2,000 calls/day) is sufficient for personal use |
| Compute (training + inference) | $0 — XGBoost trains in minutes on a laptop |
| Storage | $0 — 5–15 GB of disk for ~5,000 cached parsed matches |
| Local runtime (docker compose) | $0 |
| **Total without coach (local-only)** | **$0** |
| Claude Sonnet 4.6 via `coach` (per match) | ~$0.005–0.03 (~$0.50–2/mo at 50 games/mo) |
| *Optional:* downgrade to Haiku 4.5 (`--model haiku`) | ~$0.001 per match (~$0.05/mo) |
| *Optional:* upgrade to Opus 4.7 (`--model opus`) | ~$0.05 per match (~$2.50/mo) |
| *Optional:* OpenDota premium (faster parse queue + 5× rate limit) | $5/mo |
| *Optional:* always-on hosting (Fly.io / Railway / local server) | $5–10/mo |

## Time estimates

| Phase | Estimate | Status |
|---|---|---|
| 1. Ingest | ~6 h | done |
| 2. Training pipeline | ~8 h | done |
| 3. Decision scorer | ~8 h | done |
| 3b. Coach (LLM review + memory + farm critique + item prescription) | ~10 h | done |
| Validation (wall-clock, mostly waiting for parses) | ~12 h spread over 1 day | done |
| Hardening (cooldowns, retries, 429/5xx, explorer-lag workaround, auto-doc-sync) | ~4 h | done |
| 4. UI (Streamlit MVP) | ~3 h | done |
| 5a. Multi-user foundation + Steam OpenID + BYO key + cost gating | ~18 h | done 2026-05-17 |
| 5b. UI multi-page + onboarding + cost dashboard | ~12 h | done 2026-05-17 |
| 5c. Analyzer (counterfactual baselines, decision clustering, buybacks, replay links) | ~8 h | done 2026-05-17 |
| 5d. Coach (per-hero/matchup memory, streaming, patch tagging) | ~7 h | done 2026-05-17 |
| 5e. Model (per-bracket calibration, retrain trigger, sweep, backward-compat) | ~6 h | done 2026-05-17 |
| 5f. Validation + ops (pytest, TROUBLESHOOTING.md) | ~5 h | done 2026-05-17 |

Active dev time so far: ~99 h.

## OpenDota rate-limit considerations

Free tier: ~60 calls/min, ~2,000 calls/day.

Each match in the parse-pending cycle costs ~3 API calls (`bracket-fetch` + `request-parses` + `refresh-parses`). One pass through 500 matches = ~1,500 calls. Budget one pass per day to stay within the free quota.

If you upgrade to OpenDota premium ($5/mo), the limit goes to ~10,000 calls/day — turns "solid model" (~2k parsed matches) data-gathering from a 2–4 day exercise into <1 day.

## Security & secrets

- `.env` at the repo root is the only source of secrets and is listed in `.gitignore` — never committed.
- `ANTHROPIC_API_KEY` is a real secret — used only by the `coach` command. The code reads it from the environment, never logs it, never writes it to disk, never transmits it anywhere except the Anthropic API. Coach exits early with an actionable message if the key is missing, before any work or API call.
- `docker-compose.yml` injects `.env` into the app container via `env_file: .env`. No keys appear in the compose file, the Dockerfile, or any source file.
- Generated artifacts (`data/matches/*.json`, `data/reviews/*.md`, `data/turbo_winprob.json`, etc.) contain no secrets, just match data. The whole `data/` directory is also gitignored.
- Use `.env.example` as a template when sharing the repo — it includes the placeholder keys but no values.

## Known limitations

1. ~~Account not indexed~~ — **resolved.** "Expose Public Match Data" enabled; account now appears in OpenDota and `/recentMatches` returns matches.
2. **Parse rate is low for lower brackets.** Crusader matches are less likely to be already-parsed than Ancient/Divine — most matches need to be queued via `request-parses` and waited on. (Mitigation: parse-cooldown of 24h prevents duplicate POSTs; per-match `refresh-parses` is the reliable status check.)
3. **Replays expire after ~14 days.** Matches older than that may fail to parse permanently. Discovery sorts by `start_time DESC` to bias toward fresh matches.
4. **Patch drift.** Model is implicitly tied to the patch its training data was collected in. After a major patch, retrain.
5. **Currency of `KEY_ITEMS` list.** Hand-curated. New impactful items added in patches need a code edit.
6. ~~3N API calls per N matches~~ — **resolved.** `bracket-fetch` uses a single `/explorer` JOIN; `request-parses` cooldown prevents duplicate POSTs.
7. **OpenDota `/explorer matches` table lags reality.** Matches visible as parsed via `/matches/{id}` may not appear in the `matches` SQL table for hours. `refresh-parses` now does per-match `/matches/{id}` checks rather than relying on `/explorer` for status. Costs ~200 API calls per cycle; fits free-tier daily quota.
8. **Coach output is non-deterministic.** Sonnet 4.6 will phrase the same findings differently across calls. The factual ground truth comes from `analyze()`, so the *content* is stable, but the prose isn't. Rerun if you want a different angle.
9. **Coach prompt caching probably won't activate.** Sonnet 4.6's prompt-cache minimum is 2048 input tokens; system prompt is ~750 tokens. `cache_control` is in place but `cache_read_input_tokens` stays 0 until the prompt grows. Cost impact: minimal.
10. **Saturated at ~600 matches with current features.** More training data alone won't move `val_auc` past ~0.86 — future gains require better features (see Future work → Model).
11. ~~Coach memory is single-account.~~ **Resolved in Phase 5a.** Each account has its own `data/coach_memory/<account_id>.json` file; reviewing another player's match writes to their memory file, not yours.

## Future work — roadmap

Grouped by area. Effort rough: **S** ≤2h, **M** ~half-day, **L** day+, **XL** multi-day. None are blocking — pick from any tier based on what bugs you during use.

### Model

| Idea | Effort | Why |
|---|---|---|
| Switch hero columns to XGBoost categorical mode (`enable_categorical=True`) | S | Cleaner matchup splits; expected +0.005–0.015 `val_auc` |
| ~~**Counterfactual baselines**~~ — per-(rank, hero, item) median purchase-time distributions | — | **Done in 5c.1.** Builds with `build-baselines` CLI; coach loads and adds "vs bracket median" timing deltas to each KEY_ITEM the user bought. |
| Lane-outcome features (LH/level diff at min 10) | M | New per-side aggregate features in snapshots |
| Per-side net-worth time series | M | Currently we only have `gold_adv`; per-side curves would help fight-window detection |
| Roshan aegis-holder feature | S | Boolean: which side currently holds Aegis (objective state) |
| Item-build curve features (cumulative key items per side) | M | Captures "had BKB online" vs "still bracer-only" |
| ~~Calibration~~ | — | **Done in 5e.4** via per-bracket isotonic regression (`data/calibrators.joblib`). |
| Per-hero head model | XL | One classifier per played hero. Needs ~5× current data; probably not worth it |

### Decision types (analyze + coach)

| Idea | Effort | Why |
|---|---|---|
| Ability-level decisions (skill build inflections) | M | Currently skipped — too noisy per minute. Could surface "got ult late at level 8 instead of 6" |
| ~~Lane / role assignment as a beat~~ | — | **Done in 5c.3** — passed through `analyze.you.lane_role` and used in coach prompt. |
| Vision-coverage analysis (ward placements vs death locations) | L | Use `obs_log` + `kills_log` coordinates to flag "you died inside enemy vision" |
| ~~Buyback usage events~~ | — | **Done in 5c.3.** |
| Defensive Roshan / Aegis-snipe events | M | Currently we just count Roshan kills; add who-snatched-aegis context |

### Pipeline

| Idea | Effort | Why |
|---|---|---|
| ~~Auto-retrain trigger~~ | — | **Done in 5e.2** — `training-status` CLI + `refresh-parses` emits a hint when ≥500 new parsed since last train. (Hint, not auto-fire — keeps train explicit.) |
| **Steam Web API supplement for user-match discovery** | M (~3h) | Today we discover via `/explorer` SQL at the user's rank bracket — random matches, not the user's own. Steam Web API `GetMatchHistory(account_id)` returns the signed-in user's actual recent games. Add `fetcher.fetch_user_match_history(account_id)` so onboarding can show "here are your last 20 matches, pick one to analyze" on day one. Needs `STEAM_API_KEY` (free, register at steamcommunity.com/dev/apikey). **Does not replace OpenDota** — Steam returns end-of-game summary only, not the parsed timeline data the model needs. Hybrid: Steam for discovery + profile, OpenDota for parsed match JSON. See "Why not Steam Web API" note below. |
| ~~`--rank` flag for bracket-fetch + multi-bracket bootstrap script~~ | — | **Done 2026-05-17/18.** `bracket-fetch --rank N` + `request-parses --rank-min/--rank-max` for per-bracket parse-queue targeting. Split into `scripts/collect_data.sh` (Phases 1–3, per-bracket stuck detection) and `scripts/train_model.sh` (Phase 4 artifacts) so collection and training can be iterated independently. `scripts/wipe_data.py --full` for factory reset. |
| Live-game support via Steam Web API | XL | `GetTopLiveGame`, `GetLiveLeagueGames` + a per-second feature pipeline → real-time win-prob during a match. Big effort; covered also in "UI additions → Real-time win-prob during a game". Steam-API supplement makes this feasible without polling OpenDota's parse queue. |
| OpenDota premium support | S | `OPENDOTA_API_KEY` env var injected as query param; would 5× the rate limit if user upgrades |
| Patch-aware filtering | M | Tag matches with patch; auto-exclude pre-patch data after a major patch drop |
| Match-search by hero / friend | M | Discover not just bracket matches but specific friends' games or matches involving a hero you want to study |
| Parallel `refresh-parses` (async/aiohttp) | M | Currently serial; ~3× faster with concurrent requests within rate limit |

### Coach UX

| Idea | Effort | Why |
|---|---|---|
| ~~Streaming output~~ | — | **Done in 5d.2** — CLI `--stream` flag, Streamlit live-update placeholder. |
| ~~Multi-account memory~~ | — | **Done in Phase 5a** — memory is now per-account at `data/coach_memory/<aid>.json`. |
| ~~Patch-aware memory~~ | — | **Done in 5d.3** — memory entries tagged with patch; older-patch entries filtered out of prompt injection. |
| Memory pruning by relevance | M | Keep entries that contributed to surfaced patterns longer than one-off games |
| ~~Replay-clip timestamps~~ | — | **Done in 5c.3** — each decision now has a `replay_url` (`dota2://matchid=…&matchtime=…`). |
| Coach-driven retraining hint | M | If coach commentary repeatedly cites "model didn't capture X", surface a "consider retraining" recommendation |

### UI additions (Phase 4 MVP done — these are future enhancements)

| Idea | Effort | Why |
|---|---|---|
| Multi-page nav (analyzer / history / memory / stats) | M | Split the single-page MVP into a sidebar nav. Streamlit supports this via `pages/`. |
| Match history browser | M | List of cached matches with filtering by date / hero / result. Click → load into analyzer. |
| Coach review reader | S | Browse `data/reviews/*.md` files; filter by theme tags from memory. |
| Pattern viewer | S | Render `data/coach_memory.json` as a heat-map / chart: "you die to X N% of the time in last 20 games". |
| Live collection progress | M | Stream `collect_data.sh` progress into the UI rather than CLI. |
| In-UI parse-request triggering | S | Buttons for `bracket-fetch` / `request-parses` / `refresh-parses` from the web. |
| Real-time win-prob during a game | XL | Requires hooking into Steam live-match API + per-second feature pipeline. Big effort, real value. |
| Coach review side-by-side comparison | M | Compare reviews of two matches (e.g. last loss vs last win on same hero). |
| Export / PDF coach review | S | Download button for shareable reviews. |
| Cost / API-spend dashboard | S | Track cumulative Anthropic + OpenDota usage over time. |

### Quality / ops

| Idea | Effort | Why |
|---|---|---|
| ~~Unit tests for snapshots / decision extraction~~ | — | **Done in 5f.1.** 30 pytest cases covering analyze, snapshots, baselines, cost, crypto, auth. |
| Per-decision evaluation against held-out matches | L | Validate "BKB +6.4% impact" against actual outcomes when other variables held constant |
| Memory inspection CLI | S | `python -m app.cli memory --show` to print current `coach_memory.json` summary |
| ~~`TROUBLESHOOTING.md`~~ | — | **Done in 5f.2.** |

### Why not Steam Web API (instead of OpenDota)

Asked 2026-05-17. Recap: **Steam Web API returns match summaries (final KDA, GPM, items at end). OpenDota parses the `.dem` replay file and returns the per-minute timeline** (`radiant_gold_adv`, `purchase_log`, `kills_log`, `gold_t`/`lh_t`/`xp_t`, `obs_log`/`sen_log`, `objectives` with timestamps, `lane_role`, `buyback_log`). Our model, decision scorer, and coach all depend on the parsed timeline data — Steam alone is insufficient.

Replacing OpenDota would mean parsing replays ourselves (~150 GB raw replays, JVM/Go parser runtime, ~16 h CPU per 1000 matches) — net-zero win.

Steam Web API **is** worth adding as a **supplement** (logged as `Steam Web API supplement for user-match discovery` above) for fetching the signed-in user's own match history so onboarding can show analyzable games on day one. Does not change the OpenDota dependency.

### Skipped on purpose

| Skipped idea | Why not |
|---|---|
| End-to-end RL policy model (Tier 3) | 100× the effort, marginal gain over rules + ML scorer for personal use |
| Multi-game-mode support (All Pick, etc.) | Out of scope — Turbo-only by design; mode-specific models are cleaner |
| Pro-match analysis | Different decision distribution; would degrade personal-bracket signal |
| Continuous deployment | Personal CLI tool — no service to deploy |
| Telemetry / user analytics | Single-user; nothing to collect |
| **Replacing OpenDota with self-parsed replays** | OpenDota does the .dem parsing for free. Self-parsing means JVM/Go runtime + 150 GB raw replays + ~16 h CPU per 1000 matches. Net-zero win — would just be rebuilding OpenDota inside the app. |
- **Prompt-cache breakpoint past Sonnet 4.6's 2048-token minimum.** Once the system prompt grows (more rules, more style guidance, examples), caching will start activating; verify `usage.cache_read_input_tokens > 0` after the next prompt expansion.
