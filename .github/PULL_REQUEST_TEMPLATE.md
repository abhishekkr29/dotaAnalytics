## Summary

<!-- 1-3 sentences. What does this PR do and why? -->

## Linked issue

<!-- Closes #123 — or "no issue, low-stakes change" -->

## Test plan

- [ ] `docker compose run --rm app python -m pytest tests/ -q` passes
- [ ] `bash scripts/smoke.sh` passes (`SMOKE_ACCOUNT_ID=<your-id>` if you have parsed matches)
- [ ] Added/updated tests for any behavior change
- [ ] If a UI change, manually tested in the browser

## Scope

- [ ] One logical change per PR (refactors and features in separate PRs)
- [ ] No accidental `.env`, `data/`, or secrets committed
- [ ] Docs updated if a public surface changed (`README`, `docs/API.md`, `docs/COACH.md`, etc.)

## Breaking changes

<!-- If yes, explain. Otherwise: "none". -->
