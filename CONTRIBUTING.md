# Contributing to dotaAnalytics

Thanks for your interest — this project is small and personal, but PRs that fix bugs or improve the ML / coach surfaces are welcome.

## Quick setup

```bash
git clone https://github.com/abhishekkr29/dotaAnalytics
cd dotaAnalytics

cp .env.example .env
# Edit .env — at minimum:
#   JWT_SECRET    (any long random string)
#   FERNET_KEY    (generate via the command below)

docker compose build
docker compose run --rm app python -m app.crypto gen   # → paste output as FERNET_KEY in .env
```

You can develop fully without an Anthropic API key — `analyze`, `train`, the web UI, and the test suite all work without one. The `coach`, `recommend_per_leak`, `assign_blame`, and `chat` features need a key (paste it as `ANTHROPIC_API_KEY` in `.env` or per-user via the Settings page once signed in).

OpenDota's free tier (~2000 calls/day, no key required) is enough for development. A Premium key (`OPENDOTA_API_KEY` in `.env`) is only useful for the bulk data collection scripts.

## Running tests

```bash
docker compose run --rm app python -m pytest tests/ -q
```

The full smoke test (24+ checks across the CLI, DB schema, and auth/web services) lives at `scripts/smoke.sh`:

```bash
bash scripts/smoke.sh
# or with your own account id for the richer checks:
SMOKE_ACCOUNT_ID=<your-account-id> bash scripts/smoke.sh
```

CI runs `pytest` on every PR (see `.github/workflows/test.yml`). PRs need to be green before merge.

## Code style

- **Python**: ruff is the linter — config in `pyproject.toml`. Run `ruff check .` locally before pushing.
- **Imports**: standard library, then third-party, then `app.*`, separated by blank lines.
- **Type hints**: encouraged but not required; the codebase isn't fully typed.
- **Comments**: only when the *why* is non-obvious. Don't restate what the code does. Don't reference removed code with `# removed X here`.
- **Tests**: every behavior change touches a test under `tests/`. New tool dispatchers in `app/chat.py` get a corresponding `tests/test_chat.py` entry, etc.

## Commits + PRs

- One logical change per PR. Mixing a refactor with a feature makes review hard.
- Commit messages: short imperative subject (≤72 chars), body if needed.
- Run the test suite before pushing. CI will tell you if you forgot.
- Reference the issue (`Closes #123`) when the PR resolves one.
- Don't commit `.env`, `data/`, or anything under `data/`. `.gitignore` should handle it.

## Reporting bugs

Open an issue using the Bug template. Include:
- What you ran (CLI command, web action)
- What you expected
- What happened (paste the traceback if any)
- Output of `docker compose run --rm app python --version`

## Proposing features

Open an issue using the Feature template first — don't write the code before we've agreed on scope. The project is opinionated about scope (Turbo only, single-user CLI compatibility, etc.); a quick discussion saves you wasted work.

## Architecture pointers

- `app/analyze.py` — win-prob curve + decision scorer. The "what was a leak" logic.
- `app/coach.py` — three Claude-backed surfaces (full review, per-leak recommendations, blame). See `docs/COACH.md`.
- `app/chat.py` — agentic chat with tool use. See `docs/COACH.md` for the tool inventory.
- `app/train.py` + `app/snapshots.py` — XGBoost training + per-bracket isotonic calibration.
- `app/auth.py` — Steam OpenID 2.0 → JWT handshake (FastAPI service on :8502).
- `app/pages/*.py` — Streamlit pages. Each calls `require_login()` at the top.

For a full module breakdown see `docs/ARCHITECTURE.md`.

## Security disclosures

If you find a security issue, please **don't** open a public issue. Email or use GitHub's private security advisory feature. See `SECURITY.md` at the repo root.

## License

By contributing, you agree your contributions are licensed under the project's MIT license (see `LICENSE`).
