import os
from pathlib import Path

ACCOUNT_ID = int(os.environ["ACCOUNT_ID"]) if os.getenv("ACCOUNT_ID") else None
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://dota:dota@localhost:5432/dota")

OPENDOTA_BASE = "https://api.opendota.com/api"
TURBO_GAME_MODE = 23

DATA_DIR = Path(os.getenv("DATA_DIR", "/code/data"))
MATCHES_DIR = DATA_DIR / "matches"
REVIEWS_DIR = DATA_DIR / "reviews"
PROFILES_DIR = DATA_DIR / "profiles"
MEMORY_DIR = DATA_DIR / "coach_memory"
MODEL_PATH = DATA_DIR / "turbo_winprob.json"

for _d in (MATCHES_DIR, REVIEWS_DIR, PROFILES_DIR, MEMORY_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # server-side default key

# OpenDota Premium API key (optional). When set, every OpenDota call is
# auto-tagged with ?api_key=… for the 5× rate limit + parse-queue priority.
# Free tier works without it.
OPENDOTA_API_KEY = os.getenv("OPENDOTA_API_KEY", "")

# Auth + crypto
JWT_SECRET = os.getenv("JWT_SECRET", "")
FERNET_KEY = os.getenv("FERNET_KEY", "")

# Cost gating (server key only; BYO bypasses)
DAILY_COST_CAP_CENTS = int(os.getenv("DAILY_COST_CAP_CENTS", "25"))  # $0.25/day

# Service URLs (Steam OpenID needs absolute URLs)
STEAM_OPENID_REALM = os.getenv("STEAM_OPENID_REALM", "http://localhost:8502")
AUTH_PUBLIC_URL = os.getenv("AUTH_PUBLIC_URL", "http://localhost:8502")
WEB_PUBLIC_URL = os.getenv("WEB_PUBLIC_URL", "http://localhost:8501")


def require_account_id() -> int:
    """Resolve an account id from ACCOUNT_ID env. Used by single-user CLI flow."""
    if not ACCOUNT_ID:
        raise SystemExit("ACCOUNT_ID is not set — pass --account <id> or add it to .env")
    return ACCOUNT_ID


def resolve_account_id(explicit: int | None = None) -> int:
    """Prefer an explicit account id (CLI flag, web session); fall back to env."""
    if explicit is not None:
        return int(explicit)
    return require_account_id()


def profile_path(account_id: int) -> Path:
    return PROFILES_DIR / f"{account_id}.json"


def memory_path(account_id: int) -> Path:
    return MEMORY_DIR / f"{account_id}.json"


def reviews_dir_for(account_id: int) -> Path:
    p = REVIEWS_DIR / str(account_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


RECOMMENDATIONS_DIR = DATA_DIR / "recommendations"
RECOMMENDATIONS_DIR.mkdir(parents=True, exist_ok=True)


def recommendations_dir_for(account_id: int) -> Path:
    p = RECOMMENDATIONS_DIR / str(account_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


BLAME_DIR = DATA_DIR / "blame"
BLAME_DIR.mkdir(parents=True, exist_ok=True)


def blame_dir_for(account_id: int) -> Path:
    p = BLAME_DIR / str(account_id)
    p.mkdir(parents=True, exist_ok=True)
    return p
