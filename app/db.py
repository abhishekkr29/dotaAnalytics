import psycopg

from app import config

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS matches (
    match_id            BIGINT PRIMARY KEY,
    start_time          BIGINT      NOT NULL,
    duration            INTEGER     NOT NULL,
    game_mode           INTEGER     NOT NULL,
    lobby_type          INTEGER     NOT NULL,
    radiant_win         BOOLEAN     NOT NULL,
    avg_rank_tier       INTEGER,
    parsed              BOOLEAN     NOT NULL DEFAULT FALSE,
    patch               INTEGER,
    parse_requested_at  TIMESTAMPTZ,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_matches_parsed         ON matches(parsed);
CREATE INDEX IF NOT EXISTS idx_matches_avg_rank       ON matches(avg_rank_tier);
CREATE INDEX IF NOT EXISTS idx_matches_parse_request  ON matches(parse_requested_at);
ALTER TABLE matches ADD COLUMN IF NOT EXISTS parse_requested_at TIMESTAMPTZ;

-- v3: drop the single-user back-compat columns. Per-user "you played in this match"
-- data lives in user_matches now.
ALTER TABLE matches DROP COLUMN IF EXISTS your_slot;
ALTER TABLE matches DROP COLUMN IF EXISTS your_hero_id;
ALTER TABLE matches DROP COLUMN IF EXISTS your_kills;
ALTER TABLE matches DROP COLUMN IF EXISTS your_deaths;
ALTER TABLE matches DROP COLUMN IF EXISTS your_assists;

CREATE TABLE IF NOT EXISTS snapshots (
    match_id            BIGINT   NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    minute              INTEGER  NOT NULL,
    gold_adv            INTEGER  NOT NULL,
    xp_adv              INTEGER  NOT NULL,
    tower_kills_radiant SMALLINT NOT NULL,
    tower_kills_dire    SMALLINT NOT NULL,
    kills_radiant       SMALLINT NOT NULL,
    kills_dire          SMALLINT NOT NULL,
    roshan_kills        SMALLINT NOT NULL,
    avg_rank_tier       INTEGER,
    r_hero_1 SMALLINT, r_hero_2 SMALLINT, r_hero_3 SMALLINT, r_hero_4 SMALLINT, r_hero_5 SMALLINT,
    d_hero_1 SMALLINT, d_hero_2 SMALLINT, d_hero_3 SMALLINT, d_hero_4 SMALLINT, d_hero_5 SMALLINT,
    radiant_win         BOOLEAN  NOT NULL,
    PRIMARY KEY (match_id, minute)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_avg_rank ON snapshots(avg_rank_tier);

ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS r_hero_1 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS r_hero_2 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS r_hero_3 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS r_hero_4 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS r_hero_5 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS d_hero_1 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS d_hero_2 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS d_hero_3 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS d_hero_4 SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS d_hero_5 SMALLINT;

CREATE TABLE IF NOT EXISTS users (
    account_id              BIGINT PRIMARY KEY,
    steam_id_64             BIGINT UNIQUE,
    friend_code             TEXT,
    rank_tier               INTEGER,
    profile_json            JSONB,
    anthropic_key_encrypted TEXT,
    daily_cost_used_cents   INTEGER NOT NULL DEFAULT 0,
    daily_cost_reset_at     TIMESTAMPTZ,
    monthly_cost_used_cents INTEGER NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_steam_id ON users(steam_id_64);

CREATE TABLE IF NOT EXISTS user_matches (
    user_id   BIGINT  NOT NULL REFERENCES users(account_id) ON DELETE CASCADE,
    match_id  BIGINT  NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    slot      SMALLINT,
    hero_id   INTEGER,
    kills     INTEGER,
    deaths    INTEGER,
    assists   INTEGER,
    PRIMARY KEY (user_id, match_id)
);
CREATE INDEX IF NOT EXISTS idx_user_matches_user ON user_matches(user_id);
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(config.DATABASE_URL, autocommit=True)


def ensure_schema() -> None:
    with connect() as conn:
        conn.execute(SCHEMA_SQL)
