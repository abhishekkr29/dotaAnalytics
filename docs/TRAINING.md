# Training

Deep reference for the win-probability model: data, features, model, metrics, and how to retrain. For the high-level architecture view, see [ARCHITECTURE.md](ARCHITECTURE.md); for CLI commands, see [API.md](API.md); for scope and roadmap, see [PLANNING.md](PLANNING.md).

## What gets trained

A single XGBoost binary classifier predicting

```
P(radiant_win | game_state_at_minute_t, rank_bracket, hero_composition)
```

The model **is not** trained on your matches. It learns Turbo patterns at your bracket from other players' parsed matches. Your matches are only used at inference time by `analyze` / `coach`.

## Training data

### Source

OpenDota's `public_matches` table (game summary), LEFT-JOINed with `matches` to get parse status. Queried in one call via `/explorer`.

### Filters

| Filter | Value | Why |
|---|---|---|
| `game_mode` | 23 (Turbo) | Different tempo and gold/XP curves than All Pick — single mode keeps the model in-distribution |
| `lobby_type` | 0 or 7 | Public matchmaking only (skip bot games / customs) |
| `duration` | > 480 s | Drops leaver-aborts |
| `avg_rank_tier` | `your_rank ± 10` | Skill-bracket conditioning |
| `start_time` | DESC, recent first | Replays expire ~14 days after the match; older queries fail to parse |
| `version` (after JOIN) | NOT NULL when fetching JSON | Must be parsed (only parsed matches have per-minute timelines) |

### Volume targets

| Target | Snapshot rows (rough) | Expected `val_auc` | Use case |
|---|---|---|---|
| 50 matches | ~1k | 0.65–0.75 | Pipeline smoke only — too noisy to trust |
| **500 matches** | ~10k | **0.78–0.83** | **Recommended minimum for real use** |
| 2000 matches | ~40k | 0.83–0.88 | Solid personal model |
| 5000+ matches | ~100k+ | 0.85–0.90 | Diminishing returns |

### Patch / freshness

The model implicitly captures the current patch's metagame (gold/XP curves, item meta, hero strength). Retrain after major patches; minor balance patches usually don't require it. There is no patch ID in the feature set — the model's "patch awareness" comes from *which matches* you trained it on.

## Feature reference (19 features per row)

Authoritative list: `app/train.py:FEATURE_COLS`. Each row is one match-minute snapshot.

### Game state (8 features)

| Column | Type | Source in match JSON | Notes |
|---|---|---|---|
| `minute` | int | snapshot index | Implicit time-into-game signal |
| `gold_adv` | int | `match.radiant_gold_adv[t]` | Radiant gold − Dire gold (signed). Strongest single predictor |
| `xp_adv` | int | `match.radiant_xp_adv[t]` | XP differential (signed). Second strongest |
| `tower_kills_radiant` | int | cumulative count of `objectives[type=CHAT_MESSAGE_TOWER_KILL, team=3]` up to `t*60` | Dire towers destroyed by Radiant |
| `tower_kills_dire` | int | same with `team=2` | Radiant towers destroyed by Dire |
| `kills_radiant` | int | sum of radiant `players[*].kills_log[].time ≤ t*60` | Hero kills by Radiant |
| `kills_dire` | int | sum of dire `players[*].kills_log[].time ≤ t*60` | Hero kills by Dire |
| `roshan_kills` | int | count of `objectives[type=CHAT_MESSAGE_ROSHAN_KILL].time ≤ t*60` | Total Roshan kills (either team) |

### Skill conditioning (1 feature)

| Column | Type | Source | Notes |
|---|---|---|---|
| `avg_rank_tier` | int | mean of `players[i].rank_tier` (skipping NULL) | Bracket conditioning. 11–80 scale (Herald 1 = 11, Immortal = 80). Same value for every snapshot in a match |

### Hero composition (10 features)

| Columns | Type | Source | Notes |
|---|---|---|---|
| `r_hero_1..r_hero_5` | smallint | hero_ids of Radiant players, sorted ascending | Sorted within team to avoid permutation leakage |
| `d_hero_1..d_hero_5` | smallint | hero_ids of Dire players, sorted ascending | Same |

Hero IDs are integers (1–138+). XGBoost treats them as numeric, splitting on `hero_id < N` thresholds — coarser than true categorical features but captures matchup signal at bracket scale. See [Future work](#future-work) for the upgrade path.

## Label

`radiant_win ∈ {0, 1}` — the eventual match result. Same value for every snapshot row from the same match. The model learns to recover this label from successively earlier game states.

## Train / validation split

**GroupShuffleSplit by `match_id`** with `test_size=0.2`, `random_state=42`. **Critical** — a random row split would leak late-game states from matches the model trained on early-game states from. Group split ensures train and val share no matches.

Code: `app/train.py` → `train()` uses `sklearn.model_selection.GroupShuffleSplit`.

## Model

`xgb.XGBClassifier` with:

| Hyperparameter | Value | Rationale |
|---|---|---|
| `tree_method` | `"hist"` | Histogram-based; ~10× faster than `"exact"` on this dataset, no accuracy loss |
| `max_depth` | 6 | Standard for tabular; deeper overfits on small datasets |
| `learning_rate` | 0.05 | Slow learner + early stopping outperforms aggressive learners |
| `n_estimators` | 400 (CLI flag: `--n-estimators`) | Upper bound; early stopping usually fires earlier |
| `early_stopping_rounds` | 20 | Stop if val log-loss doesn't improve for 20 boosting rounds |
| `eval_metric` | `"logloss"` | Calibrated probabilities matter — `analyze` does Δ win-prob arithmetic |

Saved artifact: `data/turbo_winprob.json` (XGBoost native JSON serialization). Load with:

```python
import xgboost as xgb
model = xgb.XGBClassifier()
model.load_model("data/turbo_winprob.json")
```

## Metrics

Written to `data/model_meta.json` after each successful `train` run:

| Field | Meaning | Target |
|---|---|---|
| `n_rows` | Total snapshot rows used | — |
| `n_matches` | Distinct matches in dataset | ≥ 20 (else `SystemExit` from `train()`) |
| `n_train_rows` / `n_val_rows` | 80/20 group split | — |
| `val_log_loss` | Cross-entropy on validation set | Lower is better. Typical: 0.45–0.60 |
| `val_auc` | ROC-AUC on validation set | **≥ 0.78 = usable; < 0.75 = dataset too small or feature signal too weak** |
| `best_iteration` | Boosting round at which early stopping fired | Informational |
| `feature_cols` | Echo of `FEATURE_COLS` | Source of truth at training time |

`val_auc` is the headline number. Below 0.75 the Δ-scoring in `analyze` becomes too noisy to trust — go collect more data.

## Training pipeline (commands)

```bash
# 1. Discover bracket matches (1 explorer call, fetches JSONs of any already-parsed)
docker compose run --rm app python -m app.cli bracket-fetch --limit 500

# 2. Queue parse requests for unparsed matches
docker compose run --rm app python -m app.cli request-parses --limit 500

# 3. Wait ~15-30 min for OpenDota's parse queue to process

# 4. Batched parse-status check + fetch JSONs of newly-parsed matches
docker compose run --rm app python -m app.cli refresh-parses --limit 500

# 5. Repeat 2+3+4 until you have enough parsed matches (see Volume targets above)
docker compose exec -T db psql -U dota -d dota -c \
  "SELECT COUNT(*) FILTER (WHERE parsed) AS parsed, COUNT(*) AS total FROM matches;"

# 6. Extract per-minute training rows from cached JSONs
docker compose run --rm app python -m app.cli snapshots

# 7. Fit the model
docker compose run --rm app python -m app.cli train
```

The hands-off loop in [PLANNING.md](PLANNING.md) → *Validation plan* automates steps 2–4.

## Retraining

| Trigger | What to do |
|---|---|
| You accumulated more parsed matches since last train | Re-run `snapshots` + `train` (incremental; only new matches are processed by snapshots) |
| Hero feature schema changed (e.g., switched to categorical mode) | `snapshots --rebuild` + `train` |
| A major patch dropped | Let the next ~1 week of new bracket matches accumulate + parse, then `snapshots` + `train` against the freshened pool |
| `val_auc` is stuck below 0.78 | Collect more data first (target 2000 matches). If still stuck, audit feature signal — see [Gotchas](#gotchas) |

## Idempotency

Every step is resumable:

- `bracket-fetch`, `request-parses`, `refresh-parses` — DB rows upserted, parse requests deduped by OpenDota, JSON cache only fetches missing files.
- `snapshots` — only processes parsed matches that don't yet have snapshot rows; `--rebuild` forces all.
- `train` — overwrites `turbo_winprob.json` + `model_meta.json` each run; trivially re-runnable.

State lives in postgres (`pgdata` volume) and `./data/` mount. Both survive container restarts. Only `docker compose down -v` resets them.

## Gotchas

- **NULL hero columns block training.** Snapshot rows extracted before the hero-feature schema migration have NULL `r_hero_1..5` and `d_hero_1..5`. The training query filters them out (`WHERE r_hero_1 IS NOT NULL`). Run `snapshots --rebuild` to refresh them from cached JSONs.

- **Narrow rank windows can starve the dataset.** `--window 2` may not return enough matches at off-peak brackets. Default `--window 10` is fine for most cases; widen to 15 if you want more volume at the cost of bracket precision.

- **Replays expire after ~14 days.** Discovering or requesting parse for matches older than that fails silently. Discovery sorts by `start_time DESC` to bias toward fresh matches.

- **Patch drift.** A model trained on patch N tested in patch N+1 still works ~OK; trained on N tested on N+2+ degrades. Retrain after each major patch (every ~6 weeks historically).

- **Hero coverage.** Heroes that are unpopular at your bracket get few snapshots and may be poorly modeled. Not fatal — they appear only in composition columns, which the model learns to mostly ignore for low-frequency values.

- **No probability calibration step.** XGBoost's `predict_proba` with `logloss` eval is reasonably calibrated out of the box. We don't apply Platt / isotonic scaling. If the Δ-scoring in `analyze` ever feels off-calibration, adding a `CalibratedClassifierCV` wrapper in `app/train.py` is the standard fix.

- **`train` errors if fewer than 20 distinct matches** in `snapshots`. By design — you need at least that many to get a sensible GroupShuffleSplit.

## Cost

| Item | Cost |
|---|---|
| OpenDota API (free tier, ~500 calls/cycle, 3–4 cycles to fill a 500-match dataset) | $0 |
| CPU for `train` (XGBoost fit on ~10k rows × 19 features) | $0 (under 2 min on a laptop) |
| Disk for cached match JSONs (1–3 GB for 500 matches) | $0 |
| Optional OpenDota premium (5× rate limit) to compress wall-clock from 1–3 days to <1 day | $5/mo |

See [PLANNING.md](PLANNING.md) for the full project cost breakdown including coach-side LLM usage.

## Future work

- **Categorical hero features** — switch hero columns to pandas categorical dtype and pass `enable_categorical=True` to `XGBClassifier` for cleaner matchup splits. Should add ~1–2 points of `val_auc`.
- **Counterfactual baselines** (deferred). Per-(rank, hero, item) median purchase-time distributions, used as benchmarks in coach commentary. Big quality win for actionable feedback ("BKB at 17:30 — median is 14:00 at your bracket").
- **Per-hero models** — fork the trained model into 124 hero-specific heads. Too much variance for personal-scope; needs ~5x more data.
- **Probability calibration** — `CalibratedClassifierCV` if Δ-scoring needs tighter calibration.
- **Auto-retrain trigger** — fire `train` whenever N new parsed matches accumulate. Pair with a "model is stale, retrain?" warning in `coach`.

## TL;DR

- Trained on ~500 random parsed Turbo matches at your `avg_rank_tier ± 10`. Not your matches.
- 19 features per minute: 8 game-state + 1 rank + 10 hero IDs.
- Label is `radiant_win` (boolean). Group-aware 80/20 train/val split by `match_id`.
- XGBoost classifier, depth 6, lr 0.05, early-stop at 20 rounds.
- Success: `val_auc ≥ 0.78` in `data/model_meta.json`.
- CPU time: under 2 min. Wall-clock to gather data: 1–3 days on free tier.
- Idempotent and resumable — failures cost ~0 to recover from.
