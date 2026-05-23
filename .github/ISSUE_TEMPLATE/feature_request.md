---
name: Feature request
about: Suggest an idea or improvement
title: "[feature] "
labels: enhancement
---

## What problem does this solve?

<!-- One paragraph. Who is the user, what are they trying to do, why is the current workflow inadequate? -->

## Proposed solution

<!-- High level. Don't write the code yet — let's agree on scope first. -->

## Alternatives considered

<!-- Why this instead of those? -->

## Out of scope?

This project is opinionated about scope:

- Dota 2 **Turbo** only (`game_mode = 23`)
- Single-user CLI compatibility must be preserved (`--account <id>` on every command)
- Adding new ML features must consider whether they need a snapshot-table migration (`snapshots --rebuild`)

If your idea touches any of these, please flag it explicitly.
