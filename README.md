# dotaAnalytics

Personal Dota 2 Turbo decision analyzer. Pulls match data via OpenDota, trains a Turbo-specific win-probability model conditioned on your rank bracket, and ranks the in-game decisions of any single match by their impact on win probability — so you can see which calls helped and which were leaks.

**Status:** CLI-only. **Phases 1–3b (ingest, training, decision scorer, LLM coach) are done and validated end-to-end** on real user matches as of 2026-05-15. Phase 4 (UI) deferred — CLI is sufficient for personal use.

- Trained win-prob model: `val_auc 0.854` on 997 Turbo matches at your rank bracket.
- Analyze: signed Δ-win-prob attribution for 7 decision types (item / death / kill / roshan / smoke / ward_obs / ward_sen).
- Coach: ~700-word markdown reviews with item-build prescription, farm-pattern critique, timing-window counterfactuals, and cross-match memory.

Scope: **Turbo only** (`game_mode = 23`), one user, runs locally via docker compose.

## Quick start

Edit `.env` to set your `ACCOUNT_ID` (Dota friend code or 32-bit OpenDota account ID), then:

```bash
docker compose build
docker compose run --rm app python -m app.cli profile             # cache your rank
docker compose run --rm app python -m app.cli bracket-fetch --limit 500
docker compose run --rm app python -m app.cli request-parses --limit 500
# wait ~15 min; repeat refresh+request across the day until ~500 parsed
docker compose run --rm app python -m app.cli refresh-parses --limit 500
docker compose run --rm app python -m app.cli snapshots
docker compose run --rm app python -m app.cli train
docker compose run --rm app python -m app.cli analyze <match_id>
docker compose run --rm app python -m app.cli coach <match_id>     # natural-language review via Claude
```

The `coach` command additionally requires `ANTHROPIC_API_KEY` in `.env` (gitignored — never commit a key).

A smoke test for the full CLI surface lives at `scripts/smoke.sh` — run it any time to confirm wiring + graceful error paths.

Postgres comes up automatically (`depends_on` + healthcheck). Schema is bootstrapped on every CLI run.

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — system design, module breakdown, data flows, schema, design rationale.
- **[docs/TRAINING.md](docs/TRAINING.md)** — deep reference for the win-prob model: data sources, full feature schema, label, hyperparams, metrics, retraining triggers, gotchas.
- **[docs/COACH.md](docs/COACH.md)** — deep reference for the `coach` command: hybrid pipeline, prompt design, session memory schema, model selection, cost, tuning knobs.
- **[docs/PLANNING.md](docs/PLANNING.md)** — scope, phase plan, validation plan, cost analysis, known limitations, future work.
- **[docs/API.md](docs/API.md)** — CLI reference, module interfaces, OpenDota endpoints, exit codes.

## Configuration

Single `.env` file at the repo root (already in `.gitignore` — secrets stay out of GitHub):

```
ACCOUNT_ID=446619601           # your Dota friend code, or 32-bit OpenDota account id
ANTHROPIC_API_KEY=sk-ant-...   # required only for `coach`; leave blank otherwise
```

`docker-compose.yml` injects `DATABASE_URL` and `DATA_DIR` automatically. Outside docker, set them yourself.

## Layout

```
.
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env(.example)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── TRAINING.md
│   ├── COACH.md
│   ├── PLANNING.md
│   └── API.md
├── scripts/
│   ├── smoke.sh           # end-to-end CLI smoke test
│   └── train_loop.sh      # auto-paced bracket-fetch / request-parses / refresh-parses → snapshots → train
└── app/
    ├── config.py          # env-driven config
    ├── db.py              # connection + idempotent schema
    ├── fetcher.py         # OpenDota client + cache + parse mgmt
    ├── snapshots.py       # per-minute feature extraction
    ├── train.py           # XGBoost win-prob trainer
    ├── analyze.py         # win-prob curve + decision scorer
    ├── coach.py           # heuristic beats + Claude → markdown review
    └── cli.py             # entry points
```

## Cost

$0 — runs locally. OpenDota free tier (~2,000 calls/day) is sufficient for personal use. Disk: 5–15 GB for ~5,000 cached parsed matches. XGBoost trains on a laptop in minutes.

## Roadmap

| Phase | Status |
|---|---|
| 1. Ingest | done |
| 2. Training pipeline | **done** — `val_auc 0.854` on 997 matches ([docs/TRAINING.md](docs/TRAINING.md) for live metrics) |
| 3. Decision scorer | **done — validated on 2 real user matches** |
| 3b. Coach (LLM review + memory) | **done — validated** (counterfactuals, item prescription, farm critique, recurring-pattern memory across reviews) |
| Validation (play games, run pipeline end-to-end) | **done 2026-05-15** |
| 4. UI | deferred (not on active roadmap) |

See [docs/PLANNING.md](docs/PLANNING.md) for the full validation plan and future work.
