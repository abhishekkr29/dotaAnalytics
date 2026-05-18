#!/usr/bin/env bash
# Smoke test for the dotaAnalytics CLI.
#
# Exercises every subcommand: confirms wiring, schema, env, graceful errors.
# Skips checks that need real data (parsed matches, trained model, API key).
#
# Usage:   scripts/smoke.sh
# Exit:    0 if everything passed, 1 if anything failed.

set -uo pipefail

cd "$(dirname "$0")/.."

command -v docker >/dev/null 2>&1 || { echo "docker is required"; exit 1; }

PASS=0; FAIL=0; SKIP=0
FAILED=()

_dc() { docker compose run --rm app python -m app.cli "$@" 2>&1; }

# pass on exit 0
check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        printf '  [PASS] %s\n' "$name"; PASS=$((PASS+1))
    else
        printf '  [FAIL] %s\n' "$name"; FAIL=$((FAIL+1)); FAILED+=("$name")
    fi
}

# pass if stdout/stderr contains a substring
check_contains() {
    local name="$1" needle="$2"; shift 2
    local out; out="$("$@" 2>&1)" || true
    if printf '%s' "$out" | grep -qF -- "$needle"; then
        printf '  [PASS] %s\n' "$name"; PASS=$((PASS+1))
    else
        printf '  [FAIL] %s (expected substring: %q)\n' "$name" "$needle"
        printf '         got: %s\n' "$(printf '%s' "$out" | tail -1)"
        FAIL=$((FAIL+1)); FAILED+=("$name")
    fi
}

skip() { printf '  [SKIP] %s — %s\n' "$1" "$2"; SKIP=$((SKIP+1)); }

echo "=== build + bootstrap ==="
check "docker compose build (cached or fresh)" docker compose build
check_contains "CLI dispatcher exposes coach"   "coach"     _dc --help
check_contains "CLI dispatcher exposes analyze" "analyze"   _dc --help
check_contains "CLI dispatcher exposes train"   "train"     _dc --help
check "web service responds on :8501" bash -c "docker compose up -d web >/dev/null 2>&1 && sleep 3 && curl -fsS -o /dev/null http://localhost:8501"
check "auth service responds on :8502/healthz" bash -c "docker compose up -d auth >/dev/null 2>&1 && sleep 3 && curl -fsS http://localhost:8502/healthz | grep -q '\"ok\":true'"
check_contains "crypto gen produces a Fernet key" "=" docker compose run --rm app python -m app.crypto gen

echo
echo "=== positive path (no data required) ==="
check_contains "account resolves from .env" "account_id: 446619601" _dc account
check_contains "profile fetches/cached"     "rank_tier"              _dc profile
check_contains "snapshots runs cleanly"     "matches_processed"      _dc snapshots
check "matches + snapshots tables both exist" \
  bash -c "docker compose exec -T db psql -U dota -d dota -tA -c \"SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('matches','snapshots')\" | sort | tr '\n' ',' | grep -q 'matches,snapshots,'"
check "users + user_matches tables both exist" \
  bash -c "docker compose exec -T db psql -U dota -d dota -tA -c \"SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('users','user_matches')\" | sort | tr '\n' ',' | grep -q 'user_matches,users,'"
check_contains "CLI --account flag is accepted" "account_id: 1" _dc account --account 1
check_contains "request-parses --rank-min/--rank-max accepted" "--rank-min" bash -c "docker compose run --rm app python -m app.cli request-parses --help 2>&1"
check_contains "CLI exposes build-baselines" "build-baselines" _dc --help
check_contains "CLI exposes training-status" "training-status" _dc --help
check_contains "training-status reports current parsed count" "current_parsed" _dc training-status
check_contains "CLI exposes sweep" "sweep" _dc --help
check_contains "CLI --stream flag accepted by coach" "--stream" bash -c "docker compose run --rm app python -m app.cli coach --help 2>&1"
check "pytest suite passes" docker compose run --rm app pytest tests/ -q
check "snapshots has hero columns (r_hero_1, d_hero_5)" \
  bash -c "docker compose exec -T db psql -U dota -d dota -tA -c \"SELECT column_name FROM information_schema.columns WHERE table_name='snapshots' AND column_name IN ('r_hero_1','d_hero_5')\" | sort | tr '\n' ',' | grep -q 'd_hero_5,r_hero_1,'"

echo
echo "=== graceful errors (negative path) ==="
# The "no snapshots" guard is only testable when we have no snapshots
HAS_SNAPSHOTS=$(docker compose exec -T db psql -U dota -d dota -tA \
    -c "SELECT COUNT(*)>0 FROM snapshots" 2>/dev/null | tr -d '\r[:space:]')
if [ "$HAS_SNAPSHOTS" = "t" ]; then
    skip "train fails clean when no snapshots" "DB already has snapshot rows — guard not testable"
else
    check_contains "train fails clean when no snapshots" "no snapshot rows yet" _dc train
fi
# Accept either "not found on OpenDota" (when their API returns 404)
# or "currently unreachable" (when their service is degraded and returns 5xx instead).
out=$(_dc analyze 1 2>&1) || true
if printf '%s' "$out" | grep -qE '(not found on OpenDota|currently unreachable)'; then
    printf '  [PASS] analyze handles missing/unreachable match gracefully\n'; PASS=$((PASS+1))
else
    printf '  [FAIL] analyze on bogus match id 1 — unexpected output: %s\n' "$(printf '%s' "$out" | tail -1)"
    FAIL=$((FAIL+1)); FAILED+=("analyze handles missing/unreachable")
fi
# Match 8810012394 used to be unparsed in DB; after wipes its state varies — accept any
# rejection (unparsed / not-in-match / not-found / unreachable) since all of those exercise
# the analyze guards correctly.
out=$(_dc analyze 8810012394 2>&1) || true
if printf '%s' "$out" | grep -qE '(not parsed yet|is not in match|not found on OpenDota|currently unreachable|no model at)'; then
    printf '  [PASS] analyze rejects un-processable match gracefully\n'; PASS=$((PASS+1))
else
    printf '  [FAIL] analyze on 8810012394 — unexpected output: %s\n' "$(printf '%s' "$out" | tail -1)"
    FAIL=$((FAIL+1)); FAILED+=("analyze rejects un-processable match")
fi

if ! grep -E '^ANTHROPIC_API_KEY=.+$' .env >/dev/null 2>&1; then
    check_contains "coach refuses to run without API key" "ANTHROPIC_API_KEY is not set" _dc coach 8810012394
else
    skip "coach refuses to run without API key" "key is set in .env; skipping no-key check"
fi

echo
echo "=== optional end-to-end (only if data is present) ==="

if [ -f data/turbo_winprob.json ] && [ -f data/model_meta.json ]; then
    check_contains "model_meta has val_auc" "val_auc" cat data/model_meta.json
else
    skip "trained-model verification" "no model at data/turbo_winprob.json"
fi

PARSED_ID="$(docker compose exec -T db psql -U dota -d dota -tA \
    -c "SELECT match_id FROM matches WHERE parsed LIMIT 1" 2>/dev/null \
    | tr -d '\r[:space:]' | head -c 20)"

if [ -n "$PARSED_ID" ] && [ -f data/turbo_winprob.json ]; then
    # Analyze must either succeed OR exit cleanly with "not in match" (bracket data has random players)
    out=$(_dc analyze "$PARSED_ID" 2>&1) || true
    if printf '%s' "$out" | grep -qE '("win_prob_curve"|is not in match)'; then
        printf '  [PASS] analyze runs cleanly on parsed match %s\n' "$PARSED_ID"
        PASS=$((PASS+1))
    else
        printf '  [FAIL] analyze on parsed match %s — unexpected output\n' "$PARSED_ID"
        FAIL=$((FAIL+1)); FAILED+=("analyze on parsed match")
    fi
    if grep -Eq '^ANTHROPIC_API_KEY=.+$' .env; then
        skip "coach on parsed match" "ANTHROPIC_API_KEY set but bracket matches don't have you as a player; no user-played match to test"
    else
        skip "coach end-to-end" "ANTHROPIC_API_KEY empty in .env"
    fi
else
    skip "analyze/coach end-to-end" "need a parsed match in DB + a trained model"
fi

echo
echo "=== summary ==="
printf 'pass=%d  fail=%d  skip=%d\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
    echo "Failed:"
    for c in "${FAILED[@]}"; do echo "  - $c"; done
    exit 1
fi
echo "All checks passed."
