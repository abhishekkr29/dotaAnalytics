# Security Policy

## Reporting a vulnerability

If you discover a security issue in dotaAnalytics, **please do not open a public GitHub issue**.

Instead, report it privately using one of these channels:

1. **GitHub private security advisory** (preferred): https://github.com/abhishekkr29/dotaAnalytics/security/advisories/new
2. **Email**: akeziox@gmail.com

Please include:

- A description of the issue and the impact
- Steps to reproduce, or a proof-of-concept
- The commit hash / version you tested against
- Whether you've shared the issue with anyone else

You can expect an acknowledgement within a few days. After triage we'll work with you on a fix and coordinate disclosure timing.

## Scope

In scope:

- Authentication or session-fixation bugs (Steam OpenID → JWT handoff, `app/auth.py`)
- Cross-user data leakage (one signed-in user seeing another user's matches, reviews, chats, or BYO Anthropic key)
- SQL/command/SSRF injection in CLI or web endpoints
- Insecure handling of BYO Anthropic keys (`app/crypto.py`)
- Cost-cap bypass on the shared server Anthropic key (`app/cost.py`)
- Any way to read another user's `data/profiles/`, `data/coach_memory/`, `data/reviews/`, or `data/chats/` from the web layer

Out of scope:

- Issues that require root on the host running docker compose
- Default Postgres credentials (`dota:dota`) — documented as local-dev-only
- The lack of TLS on `localhost` development binds
- Anything depending on OpenDota or Anthropic outages

## Internal threat model

For the developer-facing threat-model documentation (what's stored where, encryption keys, secret rotation, network surface), see [`docs/SECURITY.md`](docs/SECURITY.md).
