-- Оповещения по идущим матчам (specs/онлайн-оповещения.md): память «по этому
-- матчу уже отправлено», чтобы не слать повторно. Внешнего ключа на fixtures нет
-- намеренно: идущий матч может быть вне наших лиг или ещё не загружен.
-- Новая таблица, блокировок нет.

CREATE TABLE live_alerts_sent (
    fixture_id          INTEGER     PRIMARY KEY,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    league_id           INTEGER,
    home_name           TEXT,
    away_name           TEXT,
    minute              SMALLINT    NOT NULL,
    goals_home          SMALLINT    NOT NULL,
    goals_away          SMALLINT    NOT NULL,
    possession_home     SMALLINT    NOT NULL,
    shots_on_goal_home  SMALLINT    NOT NULL,
    shots_on_goal_away  SMALLINT
);

COMMENT ON TABLE live_alerts_sent IS
    'Отправленные оповещения по идущим матчам: один матч — одна строка. Снимки матчей не хранятся.';

CREATE INDEX idx_live_alerts_sent_at ON live_alerts_sent (sent_at);
