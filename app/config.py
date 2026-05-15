import os
from pathlib import Path

ACCOUNT_ID = int(os.environ["ACCOUNT_ID"]) if os.getenv("ACCOUNT_ID") else None
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://dota:dota@localhost:5432/dota")

OPENDOTA_BASE = "https://api.opendota.com/api"
TURBO_GAME_MODE = 23

DATA_DIR = Path(os.getenv("DATA_DIR", "/code/data"))
MATCHES_DIR = DATA_DIR / "matches"
REVIEWS_DIR = DATA_DIR / "reviews"
PROFILE_PATH = DATA_DIR / "profile.json"
MEMORY_PATH = DATA_DIR / "coach_memory.json"
MODEL_PATH = DATA_DIR / "turbo_winprob.json"
MATCHES_DIR.mkdir(parents=True, exist_ok=True)
REVIEWS_DIR.mkdir(parents=True, exist_ok=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def require_account_id() -> int:
    if not ACCOUNT_ID:
        raise SystemExit("ACCOUNT_ID is not set — add it to .env")
    return ACCOUNT_ID
