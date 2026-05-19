#!/usr/bin/env bash
# Collect training data across all 7 rank brackets (Herald → Divine).
#
# Phase 1: discovery — bracket-fetch at each rank target. Higher-rank brackets get
#          larger candidate pools because OpenDota's public_matches table thins
#          out at higher MMR (population pyramid).
# Phase 2: targeted parse-request wave — per-bracket so sparse brackets (Divine)
#          don't starve behind a flood of Herald requests in the parse queue.
# Phase 3: refresh-parses + per-bracket targeted top-ups, with per-bracket
#          stuck-detection. A bracket marked "stalled" (no progress for STUCK_CYCLES
#          consecutive cycles) no longer blocks exit.
#
# Exits when every bracket is either DONE (hit target) or STALLED. Either way,
# the next step is `scripts/train_model.sh` to build the artifacts.
#
# Re-running picks up where it left off; every step is idempotent.
#
# Usage:
#   scripts/collect_data.sh                # 400 per bracket, ±5 window, 20-min cycles
#   scripts/collect_data.sh 400 5 1800     # same, but 30-min cycles
#   scripts/collect_data.sh 400 5 1200 96  # 96 max cycles (32h ceiling)
#   scripts/collect_data.sh 400 5 1200 96 4   # mark stalled after 4 no-progress cycles

set -uo pipefail
cd "$(dirname "$0")/.."

TARGET="${1:-500}"
WINDOW="${2:-5}"
CYCLE_SLEEP="${3:-1200}"      # 1200s = 20 min between cycles
MAX_CYCLES="${4:-96}"         # safety cap (~32h at 20-min cycles)
STUCK_CYCLES="${5:-4}"        # mark a bracket stalled after this many no-progress cycles
# Optional 6th arg: discovery-limit override. When set, every bracket uses this
# fixed discovery limit instead of the scaling array below. Useful for testing
# the script end-to-end on a tiny budget (~$0.10 with limit=50 and target=20).
DISCOVERY_OVERRIDE="${6:-}"

# 7 representative rank centers; one per medal:
#   15 Herald 5 · 25 Guardian 5 · 35 Crusader 5 · 45 Archon 5 ·
#   55 Legend 5 · 65 Ancient 5 · 75 Divine 5
RANK_TARGETS=(15 25 35 45 55 65 75)

# Discovery limit per bracket — scales with rank scarcity. Higher brackets
# need much larger candidate pools because most discovered matches at high
# MMR are old / expired / unparseable.
if [ -n "$DISCOVERY_OVERRIDE" ]; then
    DISCOVERY_LIMITS=("$DISCOVERY_OVERRIDE" "$DISCOVERY_OVERRIDE" "$DISCOVERY_OVERRIDE" "$DISCOVERY_OVERRIDE" "$DISCOVERY_OVERRIDE" "$DISCOVERY_OVERRIDE" "$DISCOVERY_OVERRIDE")
else
    DISCOVERY_LIMITS=(1000 1000 1000 1500 2500 4000 4000)
fi

ts() { date '+%Y-%m-%d %H:%M:%S'; }
dc() { docker compose run --rm app python -m app.cli "$@"; }
log() { echo "[$(ts)] $*"; }

parsed_count_for_bucket() {
    # Non-overlapping bucket count (rank/10 == bucket index)
    local bucket="$1"
    local low=$((bucket * 10))
    local high=$((bucket * 10 + 9))
    docker compose exec -T db psql -U dota -d dota -tA \
        -c "SELECT COUNT(*) FROM matches WHERE parsed
             AND avg_rank_tier BETWEEN $low AND $high" 2>/dev/null \
        | tr -d '\r[:space:]'
}

trap 'log "interrupted; state persisted, re-run to resume"; exit 130' INT TERM

opendota_tier() {
    # Probe the container's environment for OPENDOTA_API_KEY (read from .env via docker-compose).
    docker compose run --rm app python -c "
from app import config
print('Premium (10000/day, queue-priority)' if config.OPENDOTA_API_KEY else 'Free (2000/day, fair-queue)')
" 2>/dev/null | tr -d '\r'
}

log "============================================================"
log "Collect-data starting"
log "  target parsed per bracket: $TARGET"
log "  rank window:               ±$WINDOW"
log "  ranks targeted:            ${RANK_TARGETS[*]}"
log "  discovery limits:          ${DISCOVERY_LIMITS[*]}"
log "  cycle sleep:               ${CYCLE_SLEEP}s"
log "  max cycles:                $MAX_CYCLES"
log "  stuck threshold:           $STUCK_CYCLES consecutive no-progress cycles"
log "  opendota tier:             $(opendota_tier)"
log "============================================================"

# ─── Phase 0: harvest already-parsed matches first ────────────────────
# On restarts (or when /explorer is lagging), there are usually thousands of
# matches already parsed at OpenDota that our DB still says are unparsed. Pull
# them in via per-match status checks BEFORE we submit any new requests, so
# Phase 2 only POSTs for things that genuinely need parsing.
PRE_HARVEST_LIMIT=20000
log "Phase 0: harvest already-parsed matches (per-match check, up to $PRE_HARVEST_LIMIT)"
dc refresh-parses --mode per_match --limit "$PRE_HARVEST_LIMIT" \
    || log "    refresh-parses failed; continuing"

# ─── Phase 1: discovery at every bracket ──────────────────────────────
log "Phase 1: discovery at each rank target"
for i in "${!RANK_TARGETS[@]}"; do
    rank="${RANK_TARGETS[$i]}"
    dlimit="${DISCOVERY_LIMITS[$i]}"
    log "  bracket-fetch --rank $rank --window $WINDOW --limit $dlimit"
    dc bracket-fetch --rank "$rank" --window "$WINDOW" --limit "$dlimit" \
        || log "    bracket-fetch failed at rank $rank; continuing"
done

# ─── Phase 1.5: harvest again (catches newly-discovered matches that are
#                                already parsed at OpenDota — /explorer's
#                                `parsed` flag in bracket-fetch is unreliable). ──
log "Phase 1.5: post-discovery harvest of already-parsed newcomers"
dc refresh-parses --mode per_match --limit "$PRE_HARVEST_LIMIT" \
    || log "    refresh-parses failed; continuing"

# ─── Phase 2: per-bracket initial parse requests ──────────────────────
# Submit `--limit 2*TARGET` per bracket. The 24h cooldown filter inside
# request-parses prevents duplicate POSTs for matches we already requested.
# After Phases 0 and 1.5, only genuinely unparsed-AND-never-requested matches
# get submitted here.
log "Phase 2: per-bracket parse requests (skips cooldowned + parsed)"
INITIAL_REQUEST_LIMIT=$((TARGET * 2))
for i in "${!RANK_TARGETS[@]}"; do
    rank="${RANK_TARGETS[$i]}"
    low=$((rank - WINDOW))
    high=$((rank + WINDOW))
    log "  request-parses --rank-min $low --rank-max $high --limit $INITIAL_REQUEST_LIMIT"
    dc request-parses --rank-min "$low" --rank-max "$high" --limit "$INITIAL_REQUEST_LIMIT" \
        || log "    request-parses failed; continuing"
done

# ─── Phase 3: refresh loop with per-bracket stuck detection ───────────
log "Phase 3: refresh + targeted top-ups"

# Bucket index for each rank target (rank / 10), used for non-overlapping counts
BUCKETS=()
for rank in "${RANK_TARGETS[@]}"; do
    BUCKETS+=($((rank / 10)))
done

# Parallel state arrays — indices match RANK_TARGETS
prev_counts=()
stuck_counts=()
status=()
for _ in "${RANK_TARGETS[@]}"; do
    prev_counts+=("0")
    stuck_counts+=("0")
    status+=("running")
done

for cycle in $(seq 1 "$MAX_CYCLES"); do
    log "----- cycle $cycle / $MAX_CYCLES -----"
    # per_match mode: reliable. /explorer-batch is faster but its `matches` table
    # has been observed lagging by 6+ hours, missing matches OpenDota has clearly
    # parsed. Per-call cost on Premium is $0.0001 so per_match at limit=2000 per
    # cycle is ~$0.20 per cycle — acceptable.
    dc refresh-parses --mode per_match --limit 2000 || log "  refresh-parses errored; continuing"

    all_settled=true
    for i in "${!RANK_TARGETS[@]}"; do
        rank="${RANK_TARGETS[$i]}"
        bucket="${BUCKETS[$i]}"
        n=$(parsed_count_for_bucket "$bucket")
        n="${n:-0}"

        if [ "$n" -ge "$TARGET" ]; then
            status[$i]="DONE"
        else
            if [ "$n" -gt "${prev_counts[$i]}" ]; then
                stuck_counts[$i]=0
            else
                stuck_counts[$i]=$((${stuck_counts[$i]} + 1))
                if [ "${stuck_counts[$i]}" -ge "$STUCK_CYCLES" ]; then
                    status[$i]="STALLED"
                fi
            fi
        fi
        prev_counts[$i]="$n"

        printf '  rank %2d bucket %d : %4d / %-4d [%-7s]  +%d no-progress cycles\n' \
            "$rank" "$bucket" "$n" "$TARGET" "${status[$i]}" "${stuck_counts[$i]}"

        if [ "${status[$i]}" = "running" ]; then
            all_settled=false
        fi
    done

    if $all_settled; then
        log "All brackets settled (DONE or STALLED). Exiting refresh loop."
        break
    fi

    # Targeted top-up parse-requests for any brackets still running.
    # Smaller per-cycle limit (--limit 100) keeps cost low when only a few brackets
    # still need parses; the 24h cooldown prevents duplicate POSTs anyway.
    for i in "${!RANK_TARGETS[@]}"; do
        if [ "${status[$i]}" = "running" ]; then
            rank="${RANK_TARGETS[$i]}"
            low=$((rank - WINDOW))
            high=$((rank + WINDOW))
            dc request-parses --rank-min "$low" --rank-max "$high" --limit 100 \
                || log "    request-parses for rank $rank failed; continuing"
        fi
    done

    if [ "$cycle" -lt "$MAX_CYCLES" ]; then
        log "  sleeping ${CYCLE_SLEEP}s before next cycle"
        sleep "$CYCLE_SLEEP"
    fi
done

# ─── Summary ──────────────────────────────────────────────────────────
log "============================================================"
log "Collection complete. Final per-bracket parsed counts:"
total=0
for i in "${!RANK_TARGETS[@]}"; do
    rank="${RANK_TARGETS[$i]}"
    bucket="${BUCKETS[$i]}"
    n=$(parsed_count_for_bucket "$bucket")
    n="${n:-0}"
    total=$((total + n))
    printf '  rank %2d bucket %d : %4d  [%s]\n' \
        "$rank" "$bucket" "$n" "${status[$i]}"
done
log "Total unique parsed: $total"
log "============================================================"
log "Next: run \`scripts/train_model.sh\` to build snapshots + train + baselines."
