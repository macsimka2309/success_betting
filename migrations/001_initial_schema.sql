-- Начальная схема. Основание: docs/03-модель-данных.md
-- Ключи — идентификаторы API-Football (ADR-5). Время — UTC (ФТ-2).

-- ---------------------------------------------------------------- справочники

CREATE TABLE leagues (
    league_id    INTEGER     PRIMARY KEY,
    name         TEXT        NOT NULL,
    type         TEXT,
    country      TEXT,
    country_code TEXT,
    legacy_code  TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN leagues.legacy_code IS
    'Код лиги прежнего проекта (E0, F2). Нужен только для переноса из parquet.';

CREATE INDEX idx_leagues_legacy_code ON leagues (legacy_code)
    WHERE legacy_code IS NOT NULL;

CREATE TABLE league_seasons (
    league_id      INTEGER     NOT NULL REFERENCES leagues (league_id) ON DELETE RESTRICT,
    season         INTEGER     NOT NULL,
    start_date     DATE,
    end_date       DATE,
    has_events     BOOLEAN,
    has_statistics BOOLEAN,
    has_lineups    BOOLEAN,
    has_injuries   BOOLEAN,
    has_odds       BOOLEAN,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league_id, season)
);

COMMENT ON TABLE league_seasons IS
    'Покрытие данными по сезонам. По флагам сборщик решает, куда не слать запросы (ФТ-5).';

CREATE TABLE teams (
    team_id    INTEGER     PRIMARY KEY,
    name       TEXT        NOT NULL,
    country    TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE players (
    player_id  INTEGER     PRIMARY KEY,
    name       TEXT        NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bookmakers (
    bookmaker_id INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL
);

CREATE TABLE bet_types (
    bet_type_id INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL
);

-- --------------------------------------------------------------------- матчи

CREATE TABLE fixtures (
    fixture_id   INTEGER     PRIMARY KEY,
    league_id    INTEGER     NOT NULL REFERENCES leagues (league_id) ON DELETE RESTRICT,
    season       INTEGER     NOT NULL,
    kickoff_at   TIMESTAMPTZ NOT NULL,
    match_date   DATE        NOT NULL,
    round        TEXT,
    status_short TEXT,
    status_long  TEXT,
    elapsed      SMALLINT,
    home_team_id INTEGER     NOT NULL REFERENCES teams (team_id) ON DELETE RESTRICT,
    away_team_id INTEGER     NOT NULL REFERENCES teams (team_id) ON DELETE RESTRICT,
    goals_home   SMALLINT,
    goals_away   SMALLINT,
    ht_home      SMALLINT,
    ht_away      SMALLINT,
    venue_name   TEXT,
    venue_city   TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE fixtures IS
    'Матч. Исход (победа/ничья) не хранится: производная величина, считается запросом (ADR-4).';

CREATE INDEX idx_fixtures_match_date ON fixtures (match_date);
CREATE INDEX idx_fixtures_league_season ON fixtures (league_id, season);
CREATE INDEX idx_fixtures_status ON fixtures (status_short);
CREATE INDEX idx_fixtures_home_team ON fixtures (home_team_id);
CREATE INDEX idx_fixtures_away_team ON fixtures (away_team_id);

CREATE TABLE fixture_events (
    event_id         BIGSERIAL   PRIMARY KEY,
    fixture_id       INTEGER     NOT NULL REFERENCES fixtures (fixture_id) ON DELETE RESTRICT,
    team_id          INTEGER     REFERENCES teams (team_id) ON DELETE RESTRICT,
    minute           SMALLINT,
    minute_extra     SMALLINT,
    type             TEXT        NOT NULL,
    detail           TEXT,
    player_id        INTEGER     REFERENCES players (player_id) ON DELETE RESTRICT,
    assist_player_id INTEGER     REFERENCES players (player_id) ON DELETE RESTRICT,
    comments         TEXT,
    event_key        TEXT        NOT NULL UNIQUE,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN fixture_events.event_key IS
    'Ключ от дублей: у событий нет своего ID в API. Собирается из матча, минуты, команды, типа, уточнения и игрока.';

CREATE INDEX idx_events_fixture ON fixture_events (fixture_id);
CREATE INDEX idx_events_player ON fixture_events (player_id);
CREATE INDEX idx_events_type ON fixture_events (type);

CREATE TABLE fixture_statistics (
    fixture_id        INTEGER      NOT NULL REFERENCES fixtures (fixture_id) ON DELETE RESTRICT,
    team_id           INTEGER      NOT NULL REFERENCES teams (team_id) ON DELETE RESTRICT,
    shots_on_goal     SMALLINT,
    shots_off_goal    SMALLINT,
    total_shots       SMALLINT,
    blocked_shots     SMALLINT,
    shots_insidebox   SMALLINT,
    shots_outsidebox  SMALLINT,
    fouls             SMALLINT,
    corner_kicks      SMALLINT,
    offsides          SMALLINT,
    ball_possession   SMALLINT,
    yellow_cards      SMALLINT,
    red_cards         SMALLINT,
    goalkeeper_saves  SMALLINT,
    total_passes      SMALLINT,
    passes_accurate   SMALLINT,
    passes_pct        SMALLINT,
    expected_goals    NUMERIC(5, 2),
    goals_prevented   NUMERIC(5, 2),
    substitutions     SMALLINT,
    free_kicks        SMALLINT,
    assists           SMALLINT,
    counter_attacks   SMALLINT,
    cross_attacks     SMALLINT,
    goals             SMALLINT,
    goal_attempts     SMALLINT,
    throwins          SMALLINT,
    medical_treatment SMALLINT,
    fetched_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (fixture_id, team_id)
);

COMMENT ON COLUMN fixture_statistics.ball_possession IS
    'Процент владения числом: источник отдаёт строку вида "58%".';

CREATE INDEX idx_statistics_team ON fixture_statistics (team_id);

CREATE TABLE fixture_lineups (
    fixture_id INTEGER     NOT NULL REFERENCES fixtures (fixture_id) ON DELETE RESTRICT,
    team_id    INTEGER     NOT NULL REFERENCES teams (team_id) ON DELETE RESTRICT,
    formation  TEXT,
    coach_id   INTEGER,
    coach_name TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fixture_id, team_id)
);

COMMENT ON COLUMN fixture_lineups.coach_id IS
    'ID тренера в API. Отдельной таблицы нет: у тренеров своё пространство идентификаторов.';

CREATE TABLE fixture_lineup_players (
    fixture_id   INTEGER     NOT NULL,
    team_id      INTEGER     NOT NULL,
    player_id    INTEGER     NOT NULL REFERENCES players (player_id) ON DELETE RESTRICT,
    is_starter   BOOLEAN     NOT NULL,
    shirt_number SMALLINT,
    position     TEXT,
    grid         TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fixture_id, team_id, player_id),
    FOREIGN KEY (fixture_id, team_id)
        REFERENCES fixture_lineups (fixture_id, team_id) ON DELETE RESTRICT
);

CREATE INDEX idx_lineup_players_player ON fixture_lineup_players (player_id);

CREATE TABLE injuries (
    fixture_id INTEGER     NOT NULL REFERENCES fixtures (fixture_id) ON DELETE RESTRICT,
    player_id  INTEGER     NOT NULL REFERENCES players (player_id) ON DELETE RESTRICT,
    team_id    INTEGER     NOT NULL REFERENCES teams (team_id) ON DELETE RESTRICT,
    league_id  INTEGER     NOT NULL REFERENCES leagues (league_id) ON DELETE RESTRICT,
    season     INTEGER     NOT NULL,
    type       TEXT,
    reason     TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fixture_id, player_id)
);

CREATE INDEX idx_injuries_player ON injuries (player_id);
CREATE INDEX idx_injuries_team ON injuries (team_id);
CREATE INDEX idx_injuries_league_season ON injuries (league_id, season);

-- -------------------------------------------------------------- коэффициенты

CREATE TABLE odds_snapshots (
    snapshot_id       BIGSERIAL   PRIMARY KEY,
    fixture_id        INTEGER     NOT NULL REFERENCES fixtures (fixture_id) ON DELETE RESTRICT,
    taken_at          TIMESTAMPTZ NOT NULL,
    source_updated_at TIMESTAMPTZ,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, taken_at)
);

COMMENT ON TABLE odds_snapshots IS
    'Снимок линии, дважды в сутки (ФТ-10). Сохраняются только 1xBet и Pinnacle.';

CREATE INDEX idx_odds_snapshots_fixture ON odds_snapshots (fixture_id);

CREATE TABLE odds_values (
    snapshot_id  BIGINT        NOT NULL REFERENCES odds_snapshots (snapshot_id) ON DELETE CASCADE,
    bookmaker_id INTEGER       NOT NULL REFERENCES bookmakers (bookmaker_id) ON DELETE RESTRICT,
    bet_type_id  INTEGER       NOT NULL REFERENCES bet_types (bet_type_id) ON DELETE RESTRICT,
    value        TEXT          NOT NULL,
    odd          NUMERIC(10, 3) NOT NULL,
    PRIMARY KEY (snapshot_id, bookmaker_id, bet_type_id, value)
);

COMMENT ON COLUMN odds_values.value IS
    'Исход строкой: Home, Over 2.5, точный счёт 2:1. Разбор — производная операция (ADR-4).';

-- ------------------------------------------------------------------ служебные

CREATE TABLE collection_runs (
    run_id          BIGSERIAL   PRIMARY KEY,
    job_name        TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL,
    requests_used   INTEGER,
    items_processed INTEGER,
    error_text      TEXT,
    CONSTRAINT collection_runs_status_check
        CHECK (status IN ('running', 'success', 'failed', 'quota_exceeded'))
);

COMMENT ON TABLE collection_runs IS 'Журнал запусков сбора (ФТ-6).';

CREATE INDEX idx_runs_job_started ON collection_runs (job_name, started_at DESC);

CREATE TABLE fixture_fetch_state (
    fixture_id            INTEGER  PRIMARY KEY REFERENCES fixtures (fixture_id) ON DELETE RESTRICT,
    events_fetched_at     TIMESTAMPTZ,
    statistics_fetched_at TIMESTAMPTZ,
    lineups_fetched_at    TIMESTAMPTZ,
    events_attempts       SMALLINT NOT NULL DEFAULT 0,
    statistics_attempts   SMALLINT NOT NULL DEFAULT 0,
    lineups_attempts      SMALLINT NOT NULL DEFAULT 0
);

COMMENT ON TABLE fixture_fetch_state IS
    'Что по матчу уже собрано. Даёт возобновление после сбоя (ФТ-4), дозагрузку (ФТ-11) и отчёт о полноте (ФТ-7).';

CREATE INDEX idx_fetch_state_events_missing ON fixture_fetch_state (fixture_id)
    WHERE events_fetched_at IS NULL;
CREATE INDEX idx_fetch_state_statistics_missing ON fixture_fetch_state (fixture_id)
    WHERE statistics_fetched_at IS NULL;
CREATE INDEX idx_fetch_state_lineups_missing ON fixture_fetch_state (fixture_id)
    WHERE lineups_fetched_at IS NULL;
