"""One-shot parallel recovery: per-match check on all parse-requested matches.

Differences from `refresh-parses --mode per_match`:
1. Skips matches with `parse_requested_at IS NULL` — those were never requested,
   so they can't be parsed at OpenDota. Saves time and money.
2. Fires fetches concurrently (ThreadPoolExecutor, 50 workers).
3. Reads from disk cache when available — important on re-runs after a crash.
4. **Incremental upserts** — each worker upserts as it finishes so a crash
   doesn't lose accumulated work.

Usage:
    docker compose run --rm app python scripts/parallel_harvest.py [WORKERS]
    # default WORKERS=50
"""

import concurrent.futures as cf
import sys
import time

from tqdm import tqdm

from app import db, fetcher


def fetch_and_upsert(mid: int) -> tuple[int, str]:
    """Worker function: get match (from cache or network), upsert if parsed.

    Returns (match_id, status) where status is one of:
      'cached_parsed'   — was on disk, was parsed, upserted
      'cached_unparsed' — was on disk, not parsed
      'fetched_parsed'  — fetched from network, was parsed, upserted
      'fetched_unparsed' — fetched from network, not parsed
      'error: <msg>'    — fetch or upsert failed
    """
    from app import config
    cached = (config.MATCHES_DIR / f"{mid}.json").exists()
    try:
        m = fetcher.fetch_match(mid, force=False)
    except Exception as e:
        return mid, f"error: {type(e).__name__}"

    parsed = fetcher.is_parsed(m)
    if parsed:
        try:
            with db.connect() as conn:
                fetcher.upsert_match(conn, m, account_id=None)
        except Exception as e:
            return mid, f"upsert_error: {type(e).__name__}"

    prefix = "cached" if cached else "fetched"
    suffix = "parsed" if parsed else "unparsed"
    return mid, f"{prefix}_{suffix}"


def main() -> int:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 50

    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT match_id FROM matches "
            "WHERE NOT parsed AND parse_requested_at IS NOT NULL "
            "ORDER BY parse_requested_at ASC"
        ).fetchall()]

    if not ids:
        print("No requested-but-unparsed matches found. Nothing to harvest.")
        return 0

    print(f"Harvesting {len(ids):,} requested-but-unparsed matches with {workers} workers")
    print("  (reads from disk cache when available; upserts incrementally on success)")
    start = time.time()

    counts = {
        "cached_parsed": 0, "cached_unparsed": 0,
        "fetched_parsed": 0, "fetched_unparsed": 0,
        "errors": 0,
    }

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_and_upsert, mid) for mid in ids]
        with tqdm(total=len(futures), desc="harvest", unit="match") as bar:
            for fut in cf.as_completed(futures):
                _mid, status = fut.result()
                if status.startswith("error") or status.startswith("upsert_error"):
                    counts["errors"] += 1
                else:
                    counts[status] = counts.get(status, 0) + 1
                bar.update(1)
                # Periodic status in the bar postfix
                if bar.n % 500 == 0:
                    bar.set_postfix(
                        parsed=counts["cached_parsed"] + counts["fetched_parsed"],
                        errors=counts["errors"],
                    )

    elapsed = time.time() - start
    total_parsed = counts["cached_parsed"] + counts["fetched_parsed"]
    print(f"\nDone in {elapsed:.0f}s ({len(ids) / elapsed:.1f} match/sec sustained).")
    print(f"  cached_parsed:    {counts['cached_parsed']:>5}  (on disk + parsed)")
    print(f"  cached_unparsed:  {counts['cached_unparsed']:>5}  (on disk but not parsed)")
    print(f"  fetched_parsed:   {counts['fetched_parsed']:>5}  (network fetch + parsed)")
    print(f"  fetched_unparsed: {counts['fetched_unparsed']:>5}  (network fetch, not parsed)")
    print(f"  errors:           {counts['errors']:>5}")
    print(f"  TOTAL PARSED:     {total_parsed:>5}  → upserted to DB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
