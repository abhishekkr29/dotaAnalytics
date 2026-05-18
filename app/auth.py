"""Steam OpenID auth service (FastAPI).

Runs in its own container on AUTH_PUBLIC_URL. After a successful Steam sign-in,
mints a JWT (shared HMAC secret) and 302s the browser to
WEB_PUBLIC_URL/?token=<jwt>. The Streamlit app validates the JWT and stores
account_id in session.
"""

import time
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app import config, db

app = FastAPI(title="dotaAnalytics auth")

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
JWT_TTL_SECONDS = 7 * 24 * 3600
JWT_ALG = "HS256"


def make_jwt(account_id: int) -> str:
    if not config.JWT_SECRET:
        raise RuntimeError("JWT_SECRET is not set")
    payload = {"account_id": int(account_id), "exp": int(time.time()) + JWT_TTL_SECONDS}
    return jwt.encode(payload, config.JWT_SECRET, algorithm=JWT_ALG)


def verify_jwt(token: str) -> dict | None:
    if not config.JWT_SECRET:
        return None
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        return None


def _steamid64_to_account_id(steamid64: int) -> int:
    """Steam ID 64 → 32-bit Dota account_id."""
    return int(steamid64) - 76561197960265728


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "auth"}


@app.get("/auth/steam/login")
def steam_login():
    return_to = f"{config.AUTH_PUBLIC_URL}/auth/steam/callback"
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.return_to": return_to,
        "openid.realm": config.STEAM_OPENID_REALM,
        "openid.mode": "checkid_setup",
    }
    return RedirectResponse(f"{STEAM_OPENID_URL}?{urlencode(params)}")


@app.get("/auth/steam/callback")
def steam_callback(request: Request):
    params = dict(request.query_params)
    if not params.get("openid.identity"):
        return PlainTextResponse("Missing OpenID assertion", status_code=400)

    verify = {**params, "openid.mode": "check_authentication"}
    try:
        resp = httpx.post(STEAM_OPENID_URL, data=verify, timeout=10.0)
    except httpx.HTTPError as e:
        return PlainTextResponse(f"Steam verification network error: {e}", status_code=502)
    if "is_valid:true" not in resp.text:
        return PlainTextResponse("Steam OpenID verification failed", status_code=401)

    identity = params["openid.identity"]
    try:
        steamid64 = int(identity.rsplit("/", 1)[-1])
    except ValueError:
        return PlainTextResponse("Couldn't parse Steam ID from OpenID identity", status_code=400)

    account_id = _steamid64_to_account_id(steamid64)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO users (account_id, steam_id_64, last_seen_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (account_id) DO UPDATE SET
                steam_id_64  = EXCLUDED.steam_id_64,
                last_seen_at = NOW();
            """,
            (account_id, steamid64),
        )

    token = make_jwt(account_id)
    return RedirectResponse(f"{config.WEB_PUBLIC_URL}/?token={token}")


@app.on_event("startup")
def _bootstrap_schema():
    db.ensure_schema()
