-- Откат 012: убирает учёт проходимости (оценки оповещений теряются; пересчитать можно по событиям матчей).
DROP VIEW IF EXISTS alert_hit_rate;
DROP INDEX IF EXISTS idx_live_alerts_outcome;
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS resolved_at;
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS final_goals_away;
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS final_goals_home;
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS goals_after_75;
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS outcome;
