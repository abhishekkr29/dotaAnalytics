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
    your_slot           SMALLINT,
    your_hero_id        INTEGER,
    your_kills          INTEGER,
    your_deaths         INTEGER,
    your_assists        INTEGER,
    parse_requested_at  TIMESTAMPTZ,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_matches_parsed         ON matches(parsed);
CREATE INDEX IF NOT EXISTS idx_matches_avg_rank       ON matches(avg_rank_tier);
CREATE INDEX IF NOT EXISTS idx_matches_parse_request  ON matches(parse_requested_at);
ALTER TABLE matches ADD COLUMN IF NOT EXISTS parse_requested_at TIMESTAMPTZ;

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

-- Hero columns added in v2 — idempotent migrations for existing snapshots tables:
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
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(config.DATABASE_URL, autocommit=True)


def ensure_schema() -> None:
    with connect() as conn:
        conn.execute(SCHEMA_SQL)
