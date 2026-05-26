# dotaAnalytics

[![tests](https://github.com/abhishekkr29/dotaAnalytics/actions/workflows/test.yml/badge.svg)](https://github.com/abhishekkr29/dotaAnalytics/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python: 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](Dockerfile)

A self-hosted Dota 2 Turbo coaching tool that combines an XGBoost win-probability model with a Claude-powered post-mortem coach. Paste a match ID; get a per-minute win-prob curve, a ranked list of decisions by Δwp, a written coach review, and an agentic chat-with-the-narrator surface.

> **TL;DR for the curious:** the ML model is ~15k parsed Turbo matches and scores 0.80 val-AUC after per-bracket isotonic calibration. The coach attributes signed Δwp to 8 decision types (item/death/kill/roshan/smoke/ward_obs/ward_sen/buyback), filters out causal false positives, then asks Claude to write up the *why* with bracket-baseline item timings and farm snapshots. There's also a [Stanley-Parable narrator](docs/COACH.md#blame-assign_blame) feature that picks one player on the losing team and roasts them in 30–50 words.

## Features

- **Win-probability model** — rank-bracket-conditioned XGBoost + per-bracket isotonic calibration (Herald → Immortal). ~15k parsed matches; `val_auc_calibrated 0.80`.
- **Decision attribution** — signed Δ-win-prob impact for 8 decision types. Decision clustering for same-fight deaths. Causal filter drops co-event false positives.
- **Coach review** — ~700-word markdown writeup with item-build prescription, farm-pattern critique, timing-window counterfactuals, and cross-match memory. Streaming.
- **Per-leak tactical recommendations** — Haiku-generated, 1–2 sentences per leak ("QoP had Eul's @13:00; you walked in unblinked..."). ~$0.005/match.
- **Stanley-Parable narrator blame** — role-aware composite scorer picks the worst player on the losing team. 30–50 word zinger. ~$0.001/match.
- **Chat with the narrator** — agentic Q&A. Seven tools pull match + cross-match data on demand ("what could I have done about repeated deaths to QoP?"). ~$0.005/message.
- **Multi-user** — Steam OpenID sign-in, per-account memory, BYO Anthropic key for unlimited usage, server-key daily cap for everyone else.

**Scope:** Turbo only (`game_mode = 23`), self-hosted via docker compose. Anthropic features are optional — `analyze`, `train`, and the web UI all work without an API key.

## Try the demo (5 minutes, no ML setup)

A pre-trained model + supporting artifacts ships in `examples/demo/`. After cloning:

```bash
cp .env.example .env
# Edit .env — JWT_SECRET and FERNET_KEY (see below). ANTHROPIC_API_KEY optional.

docker compose build
docker compose run --rm app python -m app.crypto gen   # → paste output as FERNET_KEY in .env

bash examples/install_demo.sh                          # copies the demo bundle into data/

# Pick any parsed Turbo match ID (see examples/sample_match_ids.txt for some,
# or grab a fresh one from https://www.opendota.com — filter Game Mode = Turbo).
docker compose run --rm app python -m app.cli analyze <match_id> --account 12345
```

You'll get a JSON report with the per-minute win-prob curve and the ranked decisions. For the coach / chat / blame features, also set `ANTHROPIC_API_KEY` in `.env`.

See [`examples/README.md`](examples/README.md) for caveats (patch staleness, calibrator coverage).

## First-time setup

```bash
cp .env.example .env
# Edit .env — at minimum:
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

Pages: **Analyzer** (paste a match ID → win-prob + decisions + coach review + agentic chat with the narrator) · **History** (cached matches you played) · **Patterns** (your coach memory visualised) · **Reviews** (past markdown reviews) · **Settings** (BYO Anthropic key, daily/monthly spend, sign-out).

Web sign-in is required even in local dev — Steam OpenID round-trips through Steam's site and back to `localhost:8502`. There is no `ACCOUNT_ID` env-var auto-login or CLI fallback anywhere — `--account <id>` must be passed explicitly on every CLI subcommand.

## Bootstrapping training data across all 7 brackets

The default `bracket-fetch` discovers matches at the signed-in account's own rank. To get a model that works equally well for any player, you need training data across the whole rank spectrum. The bootstrap flow:

```bash
# 1. (Optional but recommended) Get an OpenDota Premium API key (cancellable; $5/mo)
#    for 5× rate limit (~10000 calls/day vs 2000) + parse-queue priority.
#    https://www.opendota.com/api-keys
#    Paste it into .env as `OPENDOTA_API_KEY=...` — the fetcher picks it up
#    automatically.

# 2. (Optional) Wipe user/history data. Default preserves parsed matches + model.
#    Use this if you want to reset Steam sign-ins, coach memory, and reviews
#    WITHOUT losing the hours of OpenDota work behind the parsed match dataset.
docker compose run --rm app python scripts/wipe_data.py --confirm
# → type "wipe" at the prompt.
#
#    Nuclear option: full reset including parsed matches, model, baselines.
#    Forces a full re-collect (~$1.50, hours). Don't use unless training data is bad.
docker compose run --rm app python scripts/wipe_data.py --confirm --include-matches
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
export ACCOUNT=<your-account-id>          # your 9-digit OpenDota / Dota friend code (e.g. 446619601)
docker compose run --rm app python -m app.cli profile --account $ACCOUNT
docker compose run --rm app python -m app.cli bracket-fetch --account $ACCOUNT --limit 500
docker compose run --rm app python -m app.cli request-parses --limit 500
# wait ~15 min, repeat refresh+request until enough are parsed:
docker compose run --rm app python -m app.cli refresh-parses --account $ACCOUNT --limit 500
docker compose run --rm app python -m app.cli snapshots
docker compose run --rm app python -m app.cli train
docker compose run --rm app python -m app.cli analyze --account $ACCOUNT <match_id>
docker compose run --rm app python -m app.cli coach   --account $ACCOUNT <match_id>
docker compose run --rm app python -m app.cli chat    --account $ACCOUNT <match_id> "what could I have done about Pudge?"
```

`--account` is required on every CLI subcommand that operates on a user — there is no env-var fallback. Web flows source the account from the Steam JWT.

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
│   ├── smoke.sh             # end-to-end smoke test (29 checks)
│   ├── collect_data.sh      # Phase 1-3: discover + parse + refresh across 7 brackets
│   ├── train_model.sh       # Phase 4: snapshots + train + baselines
│   ├── wipe_data.py         # destructive: user/history wipe (default) / --include-matches = factory reset
│   └── sweep.py             # one-shot XGBoost hyperparam sweep
├── tests/                          # pytest suite (67 cases across 9 files)
│   ├── conftest.py                 # synthetic match fixture + env defaults
│   ├── test_analyze.py             # decision extraction + clustering
│   ├── test_auth.py                # JWT mint/verify
│   ├── test_baselines.py           # rank bucket + format_delta
│   ├── test_blame.py               # role-aware blame picker + composite score
│   ├── test_chat.py                # agentic chat tool dispatch + JSONL persistence
│   ├── test_cost.py                # estimate_cents math
│   ├── test_crypto.py              # Fernet roundtrip
│   ├── test_recommend.py           # per-leak causal-context builder
│   └── test_snapshots.py           # per-minute extraction
└── app/
    ├── config.py          # env-driven config, per-user path helpers, account resolver
    ├── db.py              # connection + idempotent schema (4 tables)
    ├── fetcher.py         # OpenDota client + cache + parse mgmt + user_matches
    ├── snapshots.py       # per-minute feature extraction
    ├── train.py           # XGBoost trainer (shared model) + per-bracket isotonic calibration
    ├── analyze.py         # win-prob curve + decision scorer + clustering + replay links
    ├── coach.py           # heuristic beats + Claude → markdown review + per-leak recs + Stanley-Parable blame
    ├── chat.py            # agentic chat (Stanley Parable narrator) — 7 tools, JSONL history
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
| 2. Training pipeline | **done** — current model `val_auc 0.80` on 15,250 matches ([docs/TRAINING.md](docs/TRAINING.md) for live metrics) |
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
| 5g. Narrator surfaces (per-leak recommendations, Stanley-Parable blame, agentic chat with 7-tool harness) | **done 2026-05-22** |
| 6. Open-source release (LICENSE, CONTRIBUTING, CoC, CI, demo bundle, issue/PR templates) | **done 2026-05-25** |

See [docs/PLANNING.md](docs/PLANNING.md) for the full validation plan and future work.
