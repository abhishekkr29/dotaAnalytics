# Security & threat model

This is a self-hosted beta. The intended deployment is "one operator runs it for themselves and a handful of friends." Several decisions reflect that scope — they are noted explicitly so the productionization path is clear.

## Secrets

All secrets live in `.env` at the repo root, which is gitignored.

| Secret | Used by | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | server-side coach default | Charged against the server's Anthropic account. Protected by per-user daily cap (`DAILY_COST_CAP_CENTS`). |
| `JWT_SECRET` | auth ↔ web JWT signing/verification | Any random ≥32-char string. Rotating it invalidates every existing session (effectively logs everyone out). |
| `FERNET_KEY` | encrypting BYO Anthropic keys at rest | Generate with `docker compose run --rm app python -m app.crypto gen`. Rotating it makes existing BYO keys un-decryptable; users would have to re-paste. |
| `DATABASE_URL` (in compose) | DB connection | Local container; not exposed beyond the docker network unless port 5432 is published. |

`.env.example` documents the keys but ships no real values.

## Authentication

Steam OpenID 2.0 is the only sign-in method.

- The user authenticates on `steamcommunity.com` — we never see their Steam password.
- On callback we POST back to Steam with `mode=check_authentication` and require `is_valid:true` in the response. The signed assertion's `openid.identity` URL is our source of truth for the Steam ID.
- We convert Steam ID 64 → 32-bit Dota `account_id` (`steamid64 − 76561197960265728`). That's also the OpenDota account ID, so it's not a separate identifier.
- A signed JWT (HS256, `JWT_SECRET`) carries `account_id` in the payload with a 7-day expiry.
- The web app validates the JWT on every page load via `app.auth.verify_jwt`. There is no separate session store.

### Dev shortcut

`app.web_auth._auto_login_dev` will use `$ACCOUNT_ID` from `.env` if no JWT is present in the session. This is intentional — it preserves the original single-user local workflow. To disable, unset `ACCOUNT_ID` in `.env`.

### Session lifetime

JWTs are 7 days. There's no refresh, no revocation list. Closing the browser tab drops session state (the token is held in `st.session_state`, not a cookie). On a fresh tab the user re-signs-in. To force-logout *everyone*, rotate `JWT_SECRET`.

## BYO Anthropic key storage

- Pasted in `Settings → Anthropic API key`.
- Validated via a free `client.models.list()` probe before save (catches typos and disabled keys without billing the user).
- Encrypted with Fernet (`FERNET_KEY` in `.env`) and stored in `users.anthropic_key_encrypted` (BYTEA-style TEXT).
- Decrypted at coach-run time, used in-memory, never logged.
- The encryption is *at rest, with a key on the same box*. Anyone who can read the DB plus `.env` can decrypt all stored keys. This is acceptable for a single-operator beta; if you take on users you don't trust the operator with their keys, switch to a per-user passphrase or KMS.
- Removing a BYO key in Settings sets the column to NULL — the ciphertext is overwritten in place.

## Cost gating

- Each successful coach call is converted to cents via `app.cost.estimate_cents` using the model's per-token pricing.
- Server-key calls increment `users.daily_cost_used_cents` (reset at UTC midnight) and `users.monthly_cost_used_cents`.
- `cost.check_budget` is called *before* each coach run; it raises `BudgetExceeded` when the daily counter ≥ cap.
- BYO calls skip the cap check and only increment the monthly counter (for the user's own stats).

This gate prevents one user from burning the operator's entire Anthropic budget. It is not a billing system; if you accept money you need real metering.

## Database

- Postgres runs in-container, default credentials `dota:dota`. Adequate for localhost; **change for any hosted deploy**.
- No row-level security. Application code enforces "user A can't see user B's data" by scoping queries to `WHERE user_id = $session_account_id` everywhere. If you add SQL surfaces (admin views, exports), preserve this.
- `users` table holds Steam IDs and (when set) encrypted Anthropic keys. Treat dumps as PII-grade.

## OpenDota

- API calls are unauthenticated (no key required for free tier).
- Match JSONs cached to `data/matches/*.json` contain public match data — no secrets. Safe to share or back up.
- Profile JSONs (`data/profiles/<aid>.json`) contain the user's persona name, rank tier, and public match summary. Same privacy class as the user's OpenDota profile page.

## File permissions

- `data/` is a bind-mounted host directory, accessible to whoever can read the host filesystem.
- Reviews (`data/reviews/<aid>/*.md`) are per-account but not separately permissioned. Operator can read everyone's.

## Network surface

| Port | Service | Auth |
|---|---|---|
| 5432 | Postgres | DB password only — **don't expose to the public internet** |
| 8501 | Streamlit web | JWT (per-page) or `ACCOUNT_ID` env fallback |
| 8502 | Auth (FastAPI) | None on `/healthz`; OpenID redirect for `/auth/steam/login`; signed Steam callback on `/auth/steam/callback` |

For a hosted deploy: terminate TLS in front, restrict 5432 to the application network, set `STEAM_OPENID_REALM` and `AUTH_PUBLIC_URL` to your public HTTPS auth host, and put both 8501 and 8502 behind the same domain (e.g. `app.example.com` and `app.example.com/auth`).

## What is NOT in scope (yet)

- Audit logging of who-did-what
- Per-user rate limits beyond the daily cost cap
- Multi-factor auth (Steam OpenID itself is the only factor)
- Encrypted backups of `data/` or the DB
- Key rotation tooling (rotate `JWT_SECRET` or `FERNET_KEY` is "edit `.env`, restart, users re-sign-in / re-paste")
- Secret-leak detection in CI
- Compliance posture (GDPR, etc.)

These are the visible gaps before any real production launch — `docs/COMMERCIAL.md` Phase A covers most of them.
