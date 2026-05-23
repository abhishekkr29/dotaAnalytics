---
name: Bug report
about: Something is broken or behaves unexpectedly
title: "[bug] "
labels: bug
---

## What you ran

<!-- Paste the CLI command, the web action (e.g. "clicked Analyze on the History page"), or the API call. -->

## What you expected to happen

<!-- One sentence. -->

## What actually happened

<!-- Paste the full traceback / error message. Screenshots welcome for UI bugs. -->

## Environment

- OS:
- Docker version: <!-- `docker --version` -->
- Python image: <!-- `docker compose run --rm app python --version` -->
- Branch / commit:

## Have you checked?

- [ ] `docs/TROUBLESHOOTING.md` doesn't already cover this
- [ ] You're not relying on `$ACCOUNT_ID` (it was removed — `--account <id>` is required on every CLI command)
- [ ] If it's an Anthropic/Coach error, you ran `docker compose run --rm app python -m app.cli account --account <id>` and got a clean reply

## Additional context

<!-- Anything else — recent changes, related issues, hypotheses. -->
