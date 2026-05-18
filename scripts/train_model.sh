#!/usr/bin/env bash
# Build model artifacts from the data collected by scripts/collect_data.sh.
#
# Phase 4: snapshots --rebuild → train (XGBoost + per-bracket calibrators) →
#          build-baselines (per-(rank, hero, item) median timings).
#
# Idempotent. Safe to re-run after collecting more data.
#
# Usage:
#   scripts/train_model.sh

set -uo pipefail
cd "$(dirname "$0")/.."

ts() { date '+%Y-%m-%d %H:%M:%S'; }
dc() { docker compose run --rm app python -m app.cli "$@"; }
log() { echo "[$(ts)] $*"; }

trap 'log "interrupted; partial artifacts may have been written"; exit 130' INT TERM

log "============================================================"
log "Train-model starting"
log "============================================================"

# Sanity check: is there enough parsed data to train?
PARSED=$(docker compose exec -T db psql -U dota -d dota -tA \
    -c "SELECT COUNT(*) FROM matches WHERE parsed" 2>/dev/null \
    | tr -d '\r[:space:]')
if [ -z "${PARSED:-}" ] || [ "$PARSED" -lt 20 ]; then
    log "Only ${PARSED:-0} parsed matches in DB — need at least 20 to train. Run collect_data.sh first."
    exit 1
fi
log "Found $PARSED parsed matches. Proceeding."

log "Step 1/4: snapshots --rebuild  (extract per-minute training rows)"
dc snapshots --rebuild || { log "snapshots failed"; exit 1; }

log "Step 2/4: train  (XGBoost + per-bracket isotonic calibrators)"
dc train || { log "train failed"; exit 1; }

log "Step 3/4: build-baselines  (per-(rank, hero, item) median timings)"
dc build-baselines || { log "build-baselines failed"; exit 1; }

log "Step 4/4: training-status  (sanity check)"
dc training-status || true

log "============================================================"
log "Calibration coverage report:"
docker compose run --rm app python -c "
import joblib, json
from app import config
cals = joblib.load(config.DATA_DIR / 'calibrators.joblib')
meta = json.loads((config.DATA_DIR / 'model_meta.json').read_text())
print(f\"  Calibrated brackets: {sorted(cals.keys())}\")
print(f\"  (1=Herald · 2=Guardian · 3=Crusader · 4=Archon · 5=Legend · 6=Ancient · 7=Divine)\")
print(f\"  val_auc:            {meta['val_auc']:.4f}\")
print(f\"  val_auc_calibrated: {meta.get('val_auc_calibrated', meta['val_auc']):.4f}\")
print(f\"  n_matches:          {meta['n_matches']}\")
print(f\"  n_rows:             {meta['n_rows']:,}\")
"

log "============================================================"
log "Per-bracket parsed counts (final):"
docker compose exec -T db psql -U dota -d dota \
    -c "SELECT avg_rank_tier/10 AS bucket,
               COUNT(*) AS parsed_matches
          FROM matches WHERE parsed
          GROUP BY 1 ORDER BY 1;" 2>&1 | tail -20

log "============================================================"
log "Done. The model + calibrators + baselines are now in data/."
log "Verify with: docker compose run --rm app python -m app.cli analyze <match_id>"
