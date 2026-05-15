# Coach

Deep reference for the `coach` command: hybrid (rules + LLM) review pipeline, prompt design, session memory, model selection, cost, tuning. For the high-level architecture view, see [ARCHITECTURE.md](ARCHITECTURE.md); for command syntax, see [API.md](API.md); for project scope, see [PLANNING.md](PLANNING.md).

## What `coach` does

Takes a single match_id, produces a natural-language markdown coach review at `data/reviews/<match_id>.md`. Also maintains a per-account memory file at `data/coach_memory.json` that lets future reviews recognize recurring patterns across games.

**Hybrid by design:**
- **Rules (deterministic)** — extract narrative beats from `analyze()` output + raw match JSON. Phase grouping, recurring patterns, hero composition, summary stats. Same input → same beats.
- **LLM (Claude Sonnet 4.6 by default)** — synthesizes the beats into prose. Explicitly told not to invent events; bounds hallucination risk to phrasing rather than facts.

Decision-event extraction itself is shared with `analyze` — see [ARCHITECTURE.md](ARCHITECTURE.md#decision-extraction) for the source-to-event mapping.

## Pipeline

```
analyze(match_id)  →  structured findings (JSON)
        │
        ▼
_build_beats()  ←  raw match JSON (hero comp, patch, rank)
        │
        ▼
_build_user_prompt()
        │
        ▼
_load_memory()  →  inject "Recent match history" section (last 5 entries)
        │
        ▼
anthropic.Anthropic().messages.create(
    model=claude-sonnet-4-6,
    system=SYSTEM_PROMPT  (cache_control=ephemeral),
    messages=[user_prompt]
)
        │
        ▼
markdown coach review
        │
        ├──► data/reviews/<match_id>.md
        └──► _append_to_memory(): summary written to data/coach_memory.json
```

Coach never writes to the postgres `matches` or `snapshots` tables. All coach-side state is on the filesystem.

## Heuristic beats

`_build_beats()` produces a structured dict before the LLM call. Each beat is a narrative unit the LLM can phrase.

| Beat | Source | What it captures |
|---|---|---|
| **Summary** | `analyze` report + match JSON | Hero, KDA, result, team, duration, rank tier, patch |
| **Decisions by phase** | `analyze` decisions grouped by minute | `early` (≤8 min), `mid` (8–15 min), `late` (>15 min) |
| **Recurring death patterns** | `Counter` over death decisions' detail strings | "Killed 3 times by Pudge" |
| **Win-prob peaks/troughs** | curve `max`/`min` indices | "Peaked at 67% around min 12, bottomed at 23% around min 18" |
| **Teammate / enemy profiles** *(rich)* | `_player_profile` per `players[]` | Per-player: `hero`, `kda`, `gpm`, `xpm`, `net_worth`, `lane_role`, `key_items` with timings, `farm_snapshots` |
| **Farm snapshots** | `_farm_snapshots` from `gold_t` / `lh_t` / `xp_t` | Per-player `min5:Xg/Ylh/Zxp` markers at minutes 5/10/15 — lets the coach diagnose lane outcomes and identify "alone-farming" enemies |
| **Smoke events** | `players[i].purchase_log[].key == "smoke_of_deceit"` | Timeline of all smoke buys with who and when |
| **Kill timeline** | `players[i].kills_log` aggregated per minute | Compact `min N: R<r>/D<d>` per minute with at least one kill — surfaces fight pacing |
| **Win-prob curve** | `analyze` report `win_prob_curve` | Per-minute win-prob from your team's perspective, full curve |
| **Comeback note** | curve-derived | For losses: "Last realistically winnable moment: minute N". For wins: "Closest you got to losing: minute N." Empty if neither applies. |

All beats are deterministic — same match in, same beats out. The LLM never sees raw match JSON; it sees these beats only.

## Prompt structure

### System prompt

Fixed across calls, ~750 tokens. Defined as `SYSTEM_PROMPT` in `app/coach.py`.

Required output sections (in order):

1. **Opening** — 1–2 sentences (hero, result, framing)
2. **Phase-by-phase review** — early / mid / late paragraphs, naming specific enemy heroes and citing win-prob numbers
3. **What could have been done differently** — 2–3 concrete counterfactuals. Required for losses; optional for wins. Must cover at least three dimensions across the counterfactuals:
   - **Item-build counters** (timing vs enemy timings, alternative items)
   - **Farm pattern** (read `farm: min5/min10/min15` snapshots; spot lane deficits or alone-farming enemies)
   - **Timing windows** (BKB-not-online-yet → fight; smoke events → which kill; Roshan timing; tower-killing windows)
4. **Item-build prescription** — 2–4 items the player should have built or built differently, each justified vs specific enemy heroes (skip if build was correct)
5. **Three takeaways** — numbered, specific, actionable, tied to data

Key rules:
- Reference enemy heroes **by name** — never "the enemy carry" if you can say "Phantom Assassin"
- Cite real `farm: minX:Yg/Zlh/Wxp` numbers for farm critiques
- Cite enemy item timings explicitly (e.g., "BKB at 14:38")
- Don't invent events, ward positions, or stats not in the prompt
- 500–800 word target
- Memory awareness: call out recurring patterns if "Recent match history" is supplied

### User prompt (per-call)

Built from beats + memory. Sections in order:

1. **Match header** — match_id, your hero/team/KDA, result, duration, rank tier, patch
2. **Hero profiles** — your team (excluding you) + enemy team. Each player on two lines:
   - Line 1: `hero  KDA  GPM  XPM  NW  lane`
   - Line 2: `farm: min5:Xg/Ylh/Zxp  min10:...  min15:...`
   - Line 3: `items: <item>@MM:SS, ...`
3. **Win-prob curve** — compact `00:50%, 01:48%, 02:52%, ...` per minute
4. **Kill timeline** — per-minute Radiant/Dire kill counts
5. **Smoke events** — every smoke buy with who and side, sorted
6. **Scored decisions by phase** — leaks + kept-doing grouped by early/mid/late
7. **Notable patterns** — death-killer counts, win-prob peak/trough
8. **Trajectory note** — comeback opportunity or "closest to losing" line
9. **Recent match history** — last 5 prior reviews (only if memory exists)
10. **Final instruction** — "Write the coach review now. Be specific. Use the raw data..."

### Prompt caching

Top-level `cache_control={"type": "ephemeral"}` marks the last cacheable block (the `system`) for 5-minute cache. Sonnet 4.6's cache minimum is 2048 input tokens; the current system prompt is shorter, so `cache_read_input_tokens` will be 0 — the marker is in place for when the prompt grows. Not a bug.

## Session memory

Single file: `data/coach_memory.json`. Per-account state (no DB row — easier to inspect, diff, delete).

### Schema

```json
{
  "account_id": 446619601,
  "history": [
    {
      "match_id": 8810000000,
      "date": "2026-05-14",
      "hero": "Storm Spirit",
      "result": "loss",
      "kda": "9/4/12",
      "themes": [
        "leak:death",
        "leak:item",
        "repeat-deaths:Pudge",
        "kept:item"
      ]
    }
  ]
}
```

### Behaviors

- **Retention:** last 20 entries kept (`MEMORY_LIMIT`)
- **Injection:** last 5 entries injected into the next prompt (`MEMORY_IN_PROMPT`)
- **Reset:** delete `data/coach_memory.json` to clear all history

### Theme extraction

Heuristic, deterministic. `_extract_themes()` in `app/coach.py`:

| Theme tag | Source |
|---|---|
| `leak:<decision_type>` | Top 2 most-frequent types among `biggest_leaks` |
| `repeat-deaths:<hero_name>` | Killer in `biggest_leaks` deaths where count ≥ 2 |
| `kept:<decision_type>` | Top type in `kept_doing_this` |

These tags are coarse on purpose — they're hints for the LLM to spot patterns across games, not detailed claims.

## Model selection

| Alias | Model ID | Cost / call | When to use |
|---|---|---|---|
| `--model haiku` | `claude-haiku-4-5` | ~$0.001 | Quick iteration, smoke tests, cheap-to-spam reviews |
| `--model sonnet` *(default)* | `claude-sonnet-4-6` | ~$0.005–0.03 | Standard use. Best balance of Dota knowledge and cost |
| `--model opus` | `claude-opus-4-7` | ~$0.05 | End-of-week deep review, premium prose, pattern spotting |

For 50 reviews/month: Haiku ≈ $0.05/mo, Sonnet ≈ $0.25/mo, Opus ≈ $2.50/mo. Default is **Sonnet** unless you override.

## Cost

Per call on Sonnet 4.6 (richer prompt as of 2026-05-15):

| Component | Range |
|---|---|
| Input tokens (system + beats + farm snapshots + memory) | ~2,500–3,500 |
| Output tokens (markdown review with counterfactuals + item prescription) | ~1,000–2,000 |
| **Total cost** | **~$0.025–0.05** per match |

Cost echoed back in the coach JSON output:

```json
{
  "usage": {
    "input_tokens": 1842,
    "output_tokens": 612,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0
  }
}
```

For the full project cost table (OpenDota + LLM tiers + optional premium), see [PLANNING.md → Cost analysis](PLANNING.md#cost-analysis).

## Tuning knobs

### Constants in `app/coach.py`

| Symbol | Default | What it controls |
|---|---|---|
| `SYSTEM_PROMPT` | (see file) | Role, output structure, rules, tone |
| `MEMORY_LIMIT` | 20 | Max entries retained in `coach_memory.json` |
| `MEMORY_IN_PROMPT` | 5 | Entries injected into the next prompt |
| `MODEL_ALIASES` | `{haiku, sonnet, opus}` → model IDs | Tier shortcuts |
| `max_tokens` (in `messages.create`) | 3500 | Output cap per call (raised to fit longer counterfactual + prescription sections) |

### CLI flags

| Flag | Default | Effect |
|---|---|---|
| `--model {haiku,sonnet,opus}` | `sonnet` | Pick Claude tier |
| `--top-k K` | 6 | Passed to `analyze` — top-K leaks and kept-doing items shown to Claude |
| `--min-impact x` | 0.005 | Passed to `analyze` — filter decisions below this Δ win-prob magnitude |

## Security

- `ANTHROPIC_API_KEY` is read from the environment, never logged, never written to disk.
- `_require_api_key()` exits early with an actionable message if the key is missing — before any work or API call. Smoke test verifies this path.
- Coach calls only the Anthropic API. No other external services.
- Generated artifacts (`data/reviews/*.md`, `data/coach_memory.json`) contain no secrets.

For the project-wide secret-handling story, see [PLANNING.md → Security & secrets](PLANNING.md#security--secrets).

## Idempotency

- Coach is read-mostly: analyze runs in-memory, no DB writes.
- Each call overwrites `data/reviews/<match_id>.md` — re-running on the same match produces a fresh review (different prose, same facts).
- Memory append is the only persistent side effect. Re-running on the same match appends another entry to history (you'll see it duplicated in memory — usually fine for personal scope; if it bugs you, delete the latest entry manually).
- API call failures (`AuthenticationError`, `RateLimitError`, `APIStatusError`) raise `SystemExit` with a clear message before memory is mutated.

## Gotchas

- **Non-deterministic prose.** Same match, different review each call. Factual content is stable (rules-driven), prose stylistically varies. Re-run if you want a different angle.
- **Prompt caching may not activate** at current sizes. Sonnet 4.6's minimum cacheable prefix is 2048 tokens; the system prompt is below that. `cache_read_input_tokens` will be 0 — by design, not a bug.
- **Recurring-pattern surfacing needs history.** First 1–2 reviews won't have memory entries to lean on; coach will read as a one-off. Patterns emerge by the 3rd review.
- **Single-account scope.** Memory is keyed by the configured `ACCOUNT_ID`. Multi-account requires keying memory entries by `account_id` — not yet supported.
- **Cost scales with match length.** Long matches → more decisions → bigger user prompt → more input tokens. Late-game brawls are the most expensive to review.
- **Top-K guidance.** Default `--top-k 6` means up to 12 decisions (6 leaks + 6 kept-doing) reach the prompt. Bumping past 10 each adds noise without much value — Claude does a better job with fewer, more impactful events.

## Future work

- **Counterfactual baselines** (deferred): "BKB at 17:30 — median for Storm at Crusader is 14:00" framing. Requires the per-(rank, hero, item) timing tables noted in [PLANNING.md → Future work](PLANNING.md#future-work-post-validation).
- **Streaming output** — pipe the review to stdout as it's generated. Useful for Opus where latency is a few seconds.
- **Multi-account memory** — key `coach_memory.json` history entries by `account_id`, allow analyzing different accounts side-by-side.
- **Patch-aware memory** — tag entries with patch ID, optionally filter when surfacing recurring patterns so cross-patch noise doesn't pollute commentary.
- **Memory pruning by relevance** — keep entries that contributed to surfaced patterns longer than one-off games.

## TL;DR

- Hybrid: rules find facts, Claude phrases them.
- Default `claude-sonnet-4-6` (~$0.005–0.03 per match, ~$0.25/month at 50 games).
- Session memory: last 5 reviews injected into each new prompt, last 20 retained.
- Output: `data/reviews/<match_id>.md` (markdown) + `data/coach_memory.json` (state).
- Tunables: `SYSTEM_PROMPT`, `MEMORY_LIMIT`, model alias, `max_tokens`, `--top-k`, `--min-impact`.
