-- Статистика матча по таймам (specs/статистика-по-таймам.md).
-- Аддитивная миграция: новая таблица и две колонки в fixture_fetch_state.
-- Новая таблица пуста — блокировок нет; ADD COLUMN с постоянным DEFAULT в
-- PostgreSQL 11+ не перезаписывает таблицу. fixture_statistics не затрагивается.

CREATE TABLE fixture_statistics_halves (
    fixture_id       INTEGER      NOT NULL REFERENCES fixtures (fixture_id) ON DELETE RESTRICT,
    team_id          INTEGER      NOT NULL REFERENCES teams (team_id) ON DELETE RESTRICT,
    half             SMALLINT     NOT NULL CHECK (half IN (1, 2)),
    shots_on_goal    SMALLINT,
    shots_off_goal   SMALLINT,
    total_shots      SMALLINT,
    blocked_shots    SMALLINT,
    shots_insidebox  SMALLINT,
    shots_outsidebox SMALLINT,
    fouls            SMALLINT,
    corner_kicks     SMALLINT,
    offsides         SMALLINT,
    ball_possession  SMALLINT,
    yellow_cards     SMALLINT,
    red_cards        SMALLINT,
    goalkeeper_saves SMALLINT,
    total_passes     SMALLINT,
    passes_accurate  SMALLINT,
    passes_pct       SMALLINT,
    expected_goals   NUMERIC(5, 2),
    goals_prevented  NUMERIC(5, 2),
    fetched_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (fixture_id, team_id, half)
);

COMMENT ON TABLE fixture_statistics_halves IS
    'Статистика команды в матче отдельно по таймам (half = 1 или 2). Значения как отдал источник: второй тайм не вычисляется как итог минус первый (владение не складывается).';

CREATE INDEX idx_statistics_halves_team ON fixture_statistics_halves (team_id);

ALTER TABLE fixture_fetch_state ADD COLUMN halves_fetched_at TIMESTAMPTZ;
ALTER TABLE fixture_fetch_state ADD COLUMN halves_attempts SMALLINT NOT NULL DEFAULT 0;
