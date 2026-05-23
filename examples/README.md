# Demo bundle

A pre-trained model + supporting artifacts so a fresh clone can run `analyze` / `coach` / `chat` in under five minutes — no need to bootstrap 15k parsed matches first.

## What's in here

| File | Size | Purpose |
|---|---|---|
| `demo/turbo_winprob.json` | 752 KB | XGBoost win-probability model |
| `demo/calibrators.joblib` | 8 KB | Per-bracket isotonic calibrators |
| `demo/baselines.json` | 1.2 MB | Per-(rank × hero × item) median timings |
| `demo/heroes.json` | 23 KB | Hero metadata cache (avoids one OpenDota call) |
| `demo/model_meta.json` | 1.5 KB | Training metrics |
| `sample_match_ids.txt` | <1 KB | Public parsed Turbo match IDs you can analyze right away |

The match JSONs themselves are *not* shipped — `analyze` lazy-fetches and caches them from OpenDota on demand. So you only need the demo bundle plus a working internet connection.

## Install

```bash
bash examples/install_demo.sh
```

This copies the bundle into `data/`, which is where the app expects to find the model. After running it you should see:

```
data/turbo_winprob.json
data/calibrators.joblib
data/baselines.json
data/heroes.json
data/model_meta.json
```

## Try it

```bash
# Pick any parsed Turbo match (e.g. one from sample_match_ids.txt).
# --account can be any integer for analyze — the picker is only used to identify
# YOUR player in the match. If the match doesn't include that account, you'll
# get a clean "is not in match" error, which is fine for a smoke test.

docker compose run --rm app python -m app.cli analyze <match_id> --account 12345
```

For the chat/coach surfaces you also need an Anthropic key — add `ANTHROPIC_API_KEY=sk-ant-...` to `.env`, or use the web UI's Settings page after signing in.

## Caveats

- **Patch staleness.** The shipped model was trained on matches from ~2026-05. As Dota balance patches accumulate, validation AUC will drift. For up-to-date numbers, re-bootstrap (see the README's "Bootstrapping training data" section).
- **Rank bracket coverage.** Calibrators exist for brackets with ≥50 validation samples. Very high (Immortal) and very low (Herald) brackets may fall back to raw probabilities.
- **No personal data.** Match JSONs that include real players' account IDs are fetched on demand from OpenDota's public API; nothing personal is shipped in this repo.
