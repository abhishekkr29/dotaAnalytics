#!/usr/bin/env bash
# Copy the demo bundle into data/ so the app can find the pre-trained model.
# Safe to run multiple times — idempotent.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d examples/demo ]; then
    echo "examples/demo/ not found — are you in the repo root?" >&2
    exit 1
fi

mkdir -p data data/matches

# Copy each artifact; warn but don't fail if a target already exists.
for f in turbo_winprob.json calibrators.joblib baselines.json heroes.json model_meta.json; do
    src="examples/demo/$f"
    dst="data/$f"
    if [ -e "$dst" ]; then
        printf '  ⚠ data/%s already exists — skipping (delete it first to overwrite).\n' "$f"
        continue
    fi
    cp "$src" "$dst"
    printf '  ✓ installed data/%s\n' "$f"
done

echo
echo "Demo bundle installed. You can now run:"
echo "  docker compose run --rm app python -m app.cli analyze <match_id> --account 12345"
echo
echo "See examples/sample_match_ids.txt for parsed Turbo matches to try."
