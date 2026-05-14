# Planning

## Goals

A personal-scope Dota 2 Turbo decision analyzer that:

1. Trains a Turbo-specific win-probability model conditioned on player rank bracket.
2. Given any match ID, ranks the in-game decisions of a target player (you) by their measured impact on win probability.
3. Surfaces "kept doing this" (positive) and "biggest leaks" (negative) decisions in a human-readable report.
4. Synthesizes the structured findings into a natural-language coach review via Claude — saved as markdown per match.

## Out of scope

- Other game modes — only `game_mode = 23` / Turbo.
- Multi-user / SaaS deployment. One account, runs locally.
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

## Phase plan

| Phase | Status | Scope |
|---|---|---|
| **1. Ingest** | done | docker-compose, postgres schema, OpenDota fetcher with disk + DB cache. `account`, `profile`, `bracket-fetch`, `match-fetch` |
| **2. Training pipeline** | code ready, awaits data | Parse-pending workflow (`request-parses`, `refresh-parses`), snapshot extraction (`snapshots`), XGBoost training (`train`). Hero IDs included as features (5 radiant + 5 dire). Awaits ~500 parsed matches to fit a useful model. **Full reference: [TRAINING.md](TRAINING.md).** |
| **3. Decision scorer** | code ready, awaits model | `analyze <match_id>` — win-prob curve + ranked decisions JSON. Decision types: item / death / kill / roshan / smoke / ward_obs / ward_sen. Awaits a trained model + a parsed match the user is in |
| **3b. Coach (LLM review + session memory)** | code ready, awaits API key + match | `coach <match_id>` — hybrid: heuristic narrative beats + Claude Sonnet 4.6 → markdown. Maintains `data/coach_memory.json` to surface recurring patterns across reviews. |
| **4. UI** | deferred | Streamlit dashboard. Deferred until Phases 1–3b are validated end-to-end against real games the user has played |
| 5. Improvements (TBD) | not started | See "Future work" below |

## Validation plan (current focus)

Before building UI, validate the CLI end-to-end:

1. Enable *Expose Public Match Data* in Dota client (Settings → Options → Advanced).
2. Play a few Turbo games (4–8 over the next few days).
3. Wait for OpenDota to ingest, request parses as needed.
4. Run the pipeline: `bracket-fetch` → `request-parses`/`refresh-parses` → `snapshots` → `train`.
5. Confirm `data/model_meta.json` shows `val_auc ≥ 0.78`.
6. Run `analyze <match_id>` on each played match.
7. Manually check that the surfaced decisions match your recollection of the game.
8. Add `ANTHROPIC_API_KEY` to `.env`, then run `coach <match_id>` on the same matches; read the markdown reviews under `data/reviews/` and check that the coach's commentary tracks your recall.
9. Tune knobs as needed: `min_impact`, the `(t−30s, t+90s)` window, the `KEY_ITEMS` list, the coach system prompt.

Only after this passes does Phase 4 (UI) start.

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
| 3b. Coach (LLM review) | ~6 h | done |
| Validation (wall-clock, mostly waiting for parses + playing games) | 2–7 days | in progress |
| 4. UI | ~6–8 h | deferred |

Active dev time so far: ~28 h.

## OpenDota rate-limit considerations

Free tier: ~60 calls/min, ~2,000 calls/day.

Each match in the parse-pending cycle costs ~3 API calls (`bracket-fetch` + `request-parses` + `refresh-parses`). One pass through 500 matches = ~1,500 calls. Budget one pass per day to stay within the free quota.

If you upgrade to OpenDota premium ($5/mo), the limit goes to ~10,000 calls/day — turns "solid model" (~2k parsed matches) data-gathering from a 2–4 day exercise into <1 day.

## Security & secrets

- `.env` at the repo root is the only source of secrets and is listed in `.gitignore` — never committed.
- `ACCOUNT_ID` is a public OpenDota account ID (your Dota friend code), not a secret; kept in `.env` for convenience.
- `ANTHROPIC_API_KEY` is a real secret — used only by the `coach` command. The code reads it from the environment, never logs it, never writes it to disk, never transmits it anywhere except the Anthropic API. Coach exits early with an actionable message if the key is missing, before any work or API call.
- `docker-compose.yml` injects `.env` into the app container via `env_file: .env`. No keys appear in the compose file, the Dockerfile, or any source file.
- Generated artifacts (`data/matches/*.json`, `data/reviews/*.md`, `data/turbo_winprob.json`, etc.) contain no secrets, just match data. The whole `data/` directory is also gitignored.
- Use `.env.example` as a template when sharing the repo — it includes the placeholder keys but no values.

## Known limitations

1. **Account not indexed.** OpenDota currently has 0 matches indexed for the configured `ACCOUNT_ID` — likely because *Expose Public Match Data* is disabled in Dota settings, or no matches since OpenDota's last sync. The pipeline still works via bracket discovery for training; only the (currently unused) your-history-fetch is blocked.
2. **Parse rate is low for lower brackets.** Crusader matches are less likely to be already-parsed than Ancient/Divine — most matches need to be queued via `request-parses` and waited on.
3. **Replays expire after ~14 days.** Matches older than that may fail to parse permanently. Discovery sorts by `start_time DESC` to bias toward fresh matches.
4. **Patch drift.** Model is implicitly tied to the patch its training data was collected in. After a major patch, retrain.
5. **Currency of `KEY_ITEMS` list.** Hand-curated. New impactful items added in patches need a code edit.
6. ~~3N API calls per N matches~~ — **resolved.** `bracket-fetch` now uses a single `/explorer` JOIN for discovery + parse-status; `refresh-parses` batches parse-status checks ~200 IDs per call. Full match JSONs are fetched lazily, only for already-parsed matches.
7. **Coach output is non-deterministic.** Sonnet 4.6 will phrase the same findings slightly differently across calls. The factual ground truth comes from `analyze()`, so the *content* is stable, but the prose isn't. Rerun if you want a different angle.
8. **Coach prompt caching may not activate at current sizes.** Sonnet 4.6's prompt-cache minimum is 2048 input tokens; the current system prompt sits below that, so `cache_control` is in place but writes/reads will be zero. Cost impact is minimal — the marker pays off automatically once the prompt grows.

## Future work (post-validation)

- **UI** (Phase 4). Streamlit page on `:8501` — paste match ID, see win-prob chart + ranked decisions cards, render the coach markdown alongside.
- ~~API-call optimization (3N → ~1N)~~ — done.
- ~~More decision types (wards, smoke)~~ — done. Future: role/lane behavior, ability levels per minute, item-build curves.
- ~~Hero-aware features~~ — done (10 hero IDs as integer features). Future: switch to XGBoost categorical mode (`enable_categorical=True`) for cleaner matchup learning.
- **Counterfactual baseline.** "At your skill, the typical player buys BKB at 14:00 — you bought it at 17:30, costing X% win-prob." Requires per-(rank, hero, item) timing distributions; would feed coach commentary with concrete benchmarks. **Deferred** by user choice.
- **Retraining loop.** Auto-trigger `train` after N new parsed matches accumulate.
- ~~Coach session memory~~ — done. Last 20 reviewed matches persisted to `data/coach_memory.json`; last 5 injected into each subsequent coach prompt.
- **Prompt-cache breakpoint past Sonnet 4.6's 2048-token minimum.** Once the system prompt grows (more rules, more style guidance, examples), caching will start activating; verify `usage.cache_read_input_tokens > 0` after the next prompt expansion.
