# dotaAnalytics

Dota 2 Turbo decision analyzer. Pulls match data via OpenDota, trains a Turbo-specific win-probability model conditioned on the player's rank bracket, and ranks the in-game decisions of any match by their impact on win probability — so you can see which calls helped and which were leaks.

**Status:** Phases 1–4 + 5a–5b done. Multi-user web sign-in (Steam OpenID) and per-account memory + cost gating live as of 2026-05-17.

- Trained win-prob model: `val_auc 0.854` on 997 Turbo matches (one shared model, rank-conditioned).
- Analyze: signed Δ-win-prob attribution for 7 decision types (item / death / kill / roshan / smoke / ward_obs / ward_sen).
- Coach: ~700-word markdown reviews with item-build prescription, farm-pattern critique, timing-window counterfactuals, and cross-match memory — per account.
- Multi-user: sign in with Steam, daily server-key budget cap, optional BYO Anthropic key for unlimited usage.

Scope: **Turbo only** (`game_mode = 23`), self-hosted via docker compose. Personal CLI workflow still works exactly as before.

## First-time setup

```bash
cp .env.example .env
# Edit .env — at minimum:
#  - ACCOUNT_ID            (optional; only for single-user CLI fallback)
#  - ANTHROPIC_API_KEY     (optional; server-side default for the coach)
#  - JWT_SECRET            (required for web sign-in; any long random string)
#  - FERNET_KEY            (required to store BYO keys; generate below)

docker compose build

# Generate a Fernet key for BYO-key encryption-at-rest:
docker compose run --rm app python -m app.crypto gen
# → paste output as FERNET_KEY in .env

```

## Quick start — Web UI (multi-user)

```bash
docker compose up -d db auth web
open http://localhost:8501
```

Click **Sign in with Steam** (the `auth` service on `:8502` handles the OpenID handshake and mints a JWT). After sign-in you land back in the app, logged in.

Pages: **Analyzer** (paste a match ID → win-prob + decisions + coach review) · **History** (cached matches you played) · **Patterns** (your coach memory visualised) · **Reviews** (past markdown reviews) · **Settings** (BYO Anthropic key, daily/monthly spend, sign-out).

**Local-dev shortcut:** with `ACCOUNT_ID` set in `.env` and no JWT in the session, the web app auto-signs you in as that account — your original single-user workflow keeps working with zero Steam round-trip.

## Bootstrapping training data across all 7 brackets

The default `bracket-fetch` discovers matches at the signed-in account's own rank. To get a model that works equally well for any player, you need training data across the whole rank spectrum. The bootstrap flow:

```bash
# 1. (Optional but recommended) Get an OpenDota Premium API key (cancellable; $5/mo)
#    for 5× rate limit (~10000 calls/day vs 2000) + parse-queue priority.
#    https://www.opendota.com/api-keys
#    Paste it into .env as `OPENDOTA_API_KEY=...` — the fetcher picks it up
#    automatically.

# 2. (Destructive) Wipe existing training data.
#    Default: keeps user accounts, BYO keys, coach memory, past reviews.
docker compose run --rm app python scripts/wipe_data.py --confirm
# → type "wipe" at the prompt.
#
#    --full: factory reset — also wipes users, profiles, coach memory,
#    reviews, and heroes.json. Run if you want a brand-new-user experience.
docker compose run --rm app python scripts/wipe_data.py --confirm --full
# → type "wipe everything" at the prompt.

# 3. Collect data across all 7 brackets (Herald → Divine).
#    Per-bracket targeting + stuck-detection so sparse brackets (Divine) don't
#    block forever. Safe to Ctrl-C and resume; every step is idempotent.
bash scripts/collect_data.sh                     # 400 per bracket, ±5 window, 20-min cycles
# or with custom targets:
bash scripts/collect_data.sh 400 5 1800          # 30-min cycles
bash scripts/collect_data.sh 400 5 1200 96 4     # 96 max cycles, mark stalled after 4 no-progress

# 4. Build model + calibrators + baselines from collected data.
#    Separate script so you can collect more data later and re-train without
#    re-collecting from scratch.
bash scripts/train_model.sh
```

Result: ~2,000–2,800 parsed matches across 7 brackets (lower brackets always over-target due to OpenDota's population pyramid; Divine may stall under target — that's normal and the model still works). Fitted calibrators for each bracket with ≥50 val samples; baselines published for the popular (rank × hero × item) tuples.

For one-rank top-ups later (no full wipe needed), use `bracket-fetch --rank <N>` directly.

## Quick start — CLI (single-user or per-account)

```bash
docker compose run --rm app python -m app.cli profile --account 446619601
docker compose run --rm app python -m app.cli bracket-fetch --account 446619601 --limit 500
docker compose run --rm app python -m app.cli request-parses --limit 500
# wait ~15 min, repeat refresh+request until enough are parsed:
docker compose run --rm app python -m app.cli refresh-parses --account 446619601 --limit 500
docker compose run --rm app python -m app.cli snapshots
docker compose run --rm app python -m app.cli train
docker compose run --rm app python -m app.cli analyze --account 446619601 <match_id>
docker compose run --rm app python -m app.cli coach   --account 446619601 <match_id>
```

`--account` defaults to `$ACCOUNT_ID` from `.env` if omitted — so the original commands still work exactly as before for single-user setups.

A smoke test (`scripts/smoke.sh`) covers the CLI surface, the auth service, and the multi-user tables. Postgres comes up automatically; schema is bootstrapped on every CLI/service start.

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — system design, module breakdown, data flows, schema, design rationale.
- **[docs/TRAINING.md](docs/TRAINING.md)** — win-prob model deep reference: data sources, feature schema, hyperparams, retraining triggers.
- **[docs/COACH.md](docs/COACH.md)** — `coach` deep reference: hybrid pipeline, prompt design, session memory, model selection, cost.
- **[docs/PLANNING.md](docs/PLANNING.md)** — scope, phase plan, validation, cost, known limitations, future work.
- **[docs/API.md](docs/API.md)** — CLI reference, module interfaces, auth endpoints, OpenDota endpoints, exit codes.
- **[docs/SECURITY.md](docs/SECURITY.md)** — secrets, Steam OpenID, JWT, Fernet, cost-gating, network surface, what's *not* yet in scope.
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — operational gotchas: OpenDota staleness, parse queue stalls, Steam OpenID realm issues, model/feature compat.

## Configuration

Single `.env` file at the repo root (gitignored). Full reference in `.env.example`:

```
ACCOUNT_ID=446619601                 # optional; single-user CLI fallback / dev-env auto-login
ANTHROPIC_API_KEY=sk-ant-...         # server-side default key for coach; per-user BYO via Settings page
JWT_SECRET=long-random-string        # signs the auth ↔ web handshake
FERNET_KEY=base64-32-bytes           # encrypts BYO Anthropic keys at rest
DAILY_COST_CAP_CENTS=25              # per-user cap when using the server key

STEAM_OPENID_REALM=http://localhost:8502
AUTH_PUBLIC_URL=http://localhost:8502
WEB_PUBLIC_URL=http://localhost:8501
```

`docker-compose.yml` injects `DATABASE_URL` and `DATA_DIR` automatically.

## Layout

```
.
├── docker-compose.yml          # db + app + auth + web
├── Dockerfile
├── requirements.txt
├── .env(.example)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── TRAINING.md
│   ├── COACH.md
│   ├── PLANNING.md
│   ├── API.md
│   └── SECURITY.md
├── scripts/
│   ├── smoke.sh             # end-to-end smoke test
│   ├── collect_data.sh      # Phase 1-3: discover + parse + refresh across 7 brackets
│   ├── train_model.sh       # Phase 4: snapshots + train + baselines
│   ├── wipe_data.py         # destructive: wipe training data (--full = factory reset)
│   └── sweep.py             # one-shot XGBoost hyperparam sweep
├── tests/                          # pytest suite (30 cases)
│   ├── conftest.py                 # synthetic match fixture + env defaults
│   ├── test_analyze.py             # decision extraction + clustering
│   ├── test_auth.py                # JWT mint/verify
│   ├── test_baselines.py           # rank bucket + format_delta
│   ├── test_cost.py                # estimate_cents math
│   ├── test_crypto.py              # Fernet roundtrip
│   └── test_snapshots.py           # per-minute extraction
└── app/
    ├── config.py          # env-driven config, per-user path helpers, account resolver
    ├── db.py              # connection + idempotent schema (4 tables)
    ├── fetcher.py         # OpenDota client + cache + parse mgmt + user_matches
    ├── snapshots.py       # per-minute feature extraction
    ├── train.py           # XGBoost trainer (shared model) + per-bracket isotonic calibration
    ├── analyze.py         # win-prob curve + decision scorer + clustering + replay links
    ├── coach.py           # heuristic beats + Claude → markdown review (streaming + BYO key)
    ├── baselines.py       # per-(rank, hero, item) median purchase-time table
    ├── cost.py            # per-user daily/monthly Anthropic spend tracking
    ├── crypto.py          # Fernet encryption for BYO API keys
    ├── auth.py            # FastAPI :8502 — Steam OpenID 2.0 → JWT
    ├── web.py             # Streamlit entry / home (login + onboarding)
    ├── web_auth.py        # current_user(), require_login(), sidebar component
    ├── pages/             # Streamlit multi-page nav
    │   ├── 1_Analyzer.py
    │   ├── 2_History.py
    │   ├── 3_Patterns.py
    │   ├── 4_Reviews.py
    │   └── 5_Settings.py
    └── cli.py             # argparse entry points
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
| 4. UI (Streamlit MVP) | **done** — runs on `:8501` as a separate service |
| 5a. Multi-user foundation (Steam OpenID, BYO keys, cost gating, per-user data) | **done 2026-05-17** |
| 5b. UI multi-page + onboarding + cost dashboard | **done 2026-05-17** |
| 5c. Analyzer (counterfactual baselines, decision clustering, buybacks, replay links) | **done 2026-05-17** |
| 5d. Coach (per-hero/matchup memory, streaming output, patch tagging) | **done 2026-05-17** |
| 5e. Model (per-bracket isotonic calibration, retrain trigger, hyperparam sweep) | **done 2026-05-17** |
| 5f. Validation + ops (30 pytest cases, TROUBLESHOOTING.md) | **done 2026-05-17** |

See [docs/PLANNING.md](docs/PLANNING.md) for the full validation plan and future work.
