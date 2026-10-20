-- Баскетбол (specs/баскетбол-сбор-данных.md): отдельная схема `basketball` в той же БД.
-- Футбольные таблицы схемы public не затрагиваются. Всё аддитивно: новая схема и пустые
-- таблицы, блокировок нет. Ключи — естественные идентификаторы API-Basketball (ADR-5).
-- Сезон хранится текстом как отдал источник: `2025-2026` у одних лиг, `2025` у других.

CREATE SCHEMA basketball;

COMMENT ON SCHEMA basketball IS
    'Данные API-Basketball для модели тоталов. Отдельно от футбола (схема public).';

CREATE TABLE basketball.leagues (
    league_id    INTEGER     PRIMARY KEY,
    name         TEXT        NOT NULL,
    type         TEXT,
    country      TEXT,
    country_code TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE basketball.league_seasons (
    league_id        INTEGER     NOT NULL REFERENCES basketball.leagues (league_id) ON DELETE RESTRICT,
    season           TEXT        NOT NULL,
    start_date       DATE,
    end_date         DATE,
    has_team_stats   BOOLEAN     NOT NULL DEFAULT false,
    has_player_stats BOOLEAN     NOT NULL DEFAULT false,
    has_odds         BOOLEAN     NOT NULL DEFAULT false,
    has_standings    BOOLEAN     NOT NULL DEFAULT false,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (league_id, season)
);

COMMENT ON TABLE basketball.league_seasons IS
    'Флаги покрытия из каталога API. Они занижают реальное покрытие старых сезонов: статистика бывает и там, где флаг «нет».';

CREATE TABLE basketball.teams (
    team_id     INTEGER     PRIMARY KEY,
    name        TEXT        NOT NULL,
    country     TEXT,
    is_national BOOLEAN,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE basketball.players (
    player_id  INTEGER     PRIMARY KEY,
    name       TEXT        NOT NULL,
    position   TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE basketball.games (
    game_id      INTEGER     PRIMARY KEY,
    league_id    INTEGER     NOT NULL REFERENCES basketball.leagues (league_id) ON DELETE RESTRICT,
    season       TEXT        NOT NULL,
    tip_off      TIMESTAMPTZ NOT NULL,
    game_date    DATE        NOT NULL,
    stage        TEXT,
    week         TEXT,
    status_short TEXT        NOT NULL,
    status_long  TEXT,
    home_team_id INTEGER     NOT NULL REFERENCES basketball.teams (team_id) ON DELETE RESTRICT,
    away_team_id INTEGER     NOT NULL REFERENCES basketball.teams (team_id) ON DELETE RESTRICT,
    home_q1      SMALLINT,
    home_q2      SMALLINT,
    home_q3      SMALLINT,
    home_q4      SMALLINT,
    home_ot      SMALLINT,
    home_total   SMALLINT,
    away_q1      SMALLINT,
    away_q2      SMALLINT,
    away_q3      SMALLINT,
    away_q4      SMALLINT,
    away_ot      SMALLINT,
    away_total   SMALLINT,
    venue        TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN basketball.games.home_ot IS
    'Очки в овертайме (все овертаймы вместе, как отдаёт источник); итог home_total включает овертайм.';

CREATE INDEX idx_bb_games_date ON basketball.games (game_date);
CREATE INDEX idx_bb_games_league_season ON basketball.games (league_id, season);
CREATE INDEX idx_bb_games_status ON basketball.games (status_short);
CREATE INDEX idx_bb_games_home ON basketball.games (home_team_id);
CREATE INDEX idx_bb_games_away ON basketball.games (away_team_id);

CREATE TABLE basketball.team_game_stats (
    game_id        INTEGER     NOT NULL REFERENCES basketball.games (game_id) ON DELETE RESTRICT,
    team_id        INTEGER     NOT NULL REFERENCES basketball.teams (team_id) ON DELETE RESTRICT,
    fg_made        SMALLINT,
    fg_attempts    SMALLINT,
    three_made     SMALLINT,
    three_attempts SMALLINT,
    ft_made        SMALLINT,
    ft_attempts    SMALLINT,
    reb_total      SMALLINT,
    reb_offence    SMALLINT,
    reb_defense    SMALLINT,
    assists        SMALLINT,
    steals         SMALLINT,
    blocks         SMALLINT,
    turnovers      SMALLINT,
    personal_fouls SMALLINT,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, team_id)
);

COMMENT ON TABLE basketball.team_game_stats IS
    'Статистика команды в игре. Проценты не хранятся (считает запрос). Пустое значение — NULL, не ноль: у NBA с января 2026 нет подборов в нападении и защите и фолов.';

CREATE INDEX idx_bb_team_stats_team ON basketball.team_game_stats (team_id);

CREATE TABLE basketball.player_game_stats (
    game_id        INTEGER     NOT NULL REFERENCES basketball.games (game_id) ON DELETE RESTRICT,
    team_id        INTEGER     NOT NULL REFERENCES basketball.teams (team_id) ON DELETE RESTRICT,
    player_id      INTEGER     NOT NULL REFERENCES basketball.players (player_id) ON DELETE RESTRICT,
    is_starter     BOOLEAN,
    seconds_played INTEGER,
    points         SMALLINT,
    fg_made        SMALLINT,
    fg_attempts    SMALLINT,
    three_made     SMALLINT,
    three_attempts SMALLINT,
    ft_made        SMALLINT,
    ft_attempts    SMALLINT,
    reb_total      SMALLINT,
    assists        SMALLINT,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, team_id, player_id)
);

COMMENT ON COLUMN basketball.player_game_stats.seconds_played IS
    'Минуты из источника вида "32:17" переведены в секунды.';

CREATE INDEX idx_bb_player_stats_player ON basketball.player_game_stats (player_id);

CREATE TABLE basketball.fetch_state (
    game_id                 INTEGER  PRIMARY KEY REFERENCES basketball.games (game_id) ON DELETE RESTRICT,
    team_stats_fetched_at   TIMESTAMPTZ,
    player_stats_fetched_at TIMESTAMPTZ,
    team_stats_attempts     SMALLINT NOT NULL DEFAULT 0,
    player_stats_attempts   SMALLINT NOT NULL DEFAULT 0
);

COMMENT ON TABLE basketball.fetch_state IS
    'Что по игре уже собрано; максимум 3 попытки на набор (как fixture_fetch_state у футбола).';

CREATE TABLE basketball.bookmakers (
    bookmaker_id INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL
);

CREATE TABLE basketball.bet_types (
    bet_type_id INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL
);

CREATE TABLE basketball.odds_snapshots (
    snapshot_id BIGSERIAL   PRIMARY KEY,
    game_id     INTEGER     NOT NULL REFERENCES basketball.games (game_id) ON DELETE RESTRICT,
    taken_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_bb_odds_snapshots_game ON basketball.odds_snapshots (game_id, taken_at);

CREATE TABLE basketball.odds_values (
    snapshot_id  BIGINT        NOT NULL REFERENCES basketball.odds_snapshots (snapshot_id) ON DELETE RESTRICT,
    bookmaker_id INTEGER       NOT NULL REFERENCES basketball.bookmakers (bookmaker_id) ON DELETE RESTRICT,
    bet_type_id  INTEGER       NOT NULL REFERENCES basketball.bet_types (bet_type_id) ON DELETE RESTRICT,
    outcome      TEXT          NOT NULL,
    line         NUMERIC(6, 1) NOT NULL DEFAULT 0,
    odd          NUMERIC(8, 3) NOT NULL,
    PRIMARY KEY (snapshot_id, bookmaker_id, bet_type_id, outcome, line)
);

COMMENT ON COLUMN basketball.odds_values.outcome IS
    'Исход: Over, Under, Home, Away.';
COMMENT ON COLUMN basketball.odds_values.line IS
    'Линия тотала (219.5); 0 для рынков без линии (победитель матча).';
