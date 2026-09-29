-- Откат 003: убирает витрину признаков и счёт основного времени.
-- Данные fixtures/events/statistics не затрагиваются.

DROP TABLE IF EXISTS ml_team_elo_state;
DROP TABLE IF EXISTS ml_match_features;
ALTER TABLE fixtures DROP COLUMN IF EXISTS ft90_away;
ALTER TABLE fixtures DROP COLUMN IF EXISTS ft90_home;
