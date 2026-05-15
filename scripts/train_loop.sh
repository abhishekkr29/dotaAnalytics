#!/usr/bin/env bash
# Auto-paced training loop: bracket-fetch → request-parses → cycles of
# refresh-parses + request-parses → snapshots → train.
#
# Stops at TARGET parsed matches, or after MAX_CYCLES if the queue is slow.
# Safe to Ctrl-C and re-run — every step is idempotent.
#
# Usage:
#   scripts/train_loop.sh                  # defaults: target 500, window 10
#   scripts/train_loop.sh 300              # smaller target
#   scripts/train_loop.sh 500 15           # wider rank window
#   scripts/train_loop.sh 500 10 900       # 15-min cycles instead of 20

set -uo pipefail

cd "$(dirname "$0")/.."

TARGET="${1:-500}"
WINDOW="${2:-10}"
SLEEP_BETWEEN="${3:-1200}"   # 1200s = 20 min between cycles
MAX_CYCLES="${4:-24}"        # safety cap; ~8h at 20-min cycles

ts() { date '+%H:%M:%S'; }
dc() { docker compose run --rm app python -m app.cli "$@"; }
parsed_count() {
    docker compose exec -T db psql -U dota -d dota -tA \
        -c "SELECT COUNT(*) FROM matches WHERE parsed" 2>/dev/null \
        | tr -d '\r[:space:]'
}

log() { echo "[$(ts)] $*"; }

trap 'log "interrupted — partial state is persisted; re-run to resume"; exit 130' INT TERM

log "============================================================"
log "Training loop starting"
log "  target:          $TARGET parsed matches"
log "  rank window:     ±$WINDOW"
log "  cycle interval:  ${SLEEP_BETWEEN}s"
log "  max cycles:      $MAX_CYCLES"
log "============================================================"

# --- Cycle 0: initial discovery + first parse wave ----------------------
log "Initial bracket-fetch (1 explorer call + lazy JSON fetches)..."
dc bracket-fetch --limit "$TARGET" --window "$WINDOW" || log "bracket-fetch failed; continuing"

log "Initial request-parses..."
dc request-parses --limit "$TARGET" || log "request-parses failed; continuing"

# --- Main loop ----------------------------------------------------------
cycle=1
while [ "$cycle" -le "$MAX_CYCLES" ]; do
    parsed="$(parsed_count)"; parsed="${parsed:-0}"
    log "── Cycle $cycle  |  parsed=$parsed / target=$TARGET ──"

    if [ "$parsed" -ge "$TARGET" ]; then
        log "Target reached. Moving to training."
        break
    fi

    log "Sleeping ${SLEEP_BETWEEN}s while OpenDota processes the parse queue..."
    sleep "$SLEEP_BETWEEN"

    log "refresh-parses (per-match /matches/{id} check; up to 200 per cycle)..."
    dc refresh-parses --limit 200 || log "refresh-parses failed; continuing"

    log "request-parses (top up unparsed)..."
    dc request-parses --limit "$TARGET" || log "request-parses failed; continuing"

    cycle=$((cycle + 1))
done

if [ "$cycle" -gt "$MAX_CYCLES" ]; then
    log "Hit MAX_CYCLES ($MAX_CYCLES) without reaching target. Training on what we have."
fi

# --- Snapshots + train -----------------------------------------------
parsed="$(parsed_count)"; parsed="${parsed:-0}"
log "============================================================"
log "Final parsed count: $parsed"

if [ "$parsed" -lt 20 ]; then
    log "Fewer than 20 parsed matches. train() will refuse — stopping."
    exit 1
fi

log "Building snapshot rows..."
dc snapshots

log "Training XGBoost model..."
dc train

log "============================================================"
log "Done. Inspect data/model_meta.json for val_auc:"
echo
cat data/model_meta.json 2>/dev/null || log "model_meta.json missing"
echo
log "Re-run this script anytime to top up data and retrain."
