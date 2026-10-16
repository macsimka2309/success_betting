-- Оповещения смотрят обе команды матча, не только хозяев (16.10.2026,
-- specs/онлайн-оповещения.md). Колонки теперь описывают команду, по которой
-- сработал триггер; team_side говорит, хозяева это или гости. Существующие строки
-- (если есть) были только по хозяевам. Переименование и ADD COLUMN с постоянным
-- DEFAULT на маленькой таблице блокировок не создают.

ALTER TABLE live_alerts_sent RENAME COLUMN possession_home TO possession;
ALTER TABLE live_alerts_sent RENAME COLUMN shots_on_goal_home TO shots_on_goal;
ALTER TABLE live_alerts_sent RENAME COLUMN shots_on_goal_away TO opponent_shots_on_goal;
ALTER TABLE live_alerts_sent ADD COLUMN team_side TEXT NOT NULL DEFAULT 'home'
    CHECK (team_side IN ('home', 'away'));
