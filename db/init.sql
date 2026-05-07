CREATE TABLE IF NOT EXISTS matches (
    match_id        BIGINT PRIMARY KEY,
    start_time      BIGINT      NOT NULL,
    duration        INTEGER     NOT NULL,
    game_mode       INTEGER     NOT NULL,
    lobby_type      INTEGER     NOT NULL,
    radiant_win     BOOLEAN     NOT NULL,
    avg_rank_tier   INTEGER,
    parsed          BOOLEAN     NOT NULL DEFAULT FALSE,
    patch           INTEGER,
    your_slot       SMALLINT,
    your_hero_id    INTEGER,
    your_kills      INTEGER,
    your_deaths     INTEGER,
    your_assists    INTEGER,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_matches_parsed   ON matches(parsed);
CREATE INDEX IF NOT EXISTS idx_matches_avg_rank ON matches(avg_rank_tier);
