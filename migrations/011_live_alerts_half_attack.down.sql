-- Откат 011: удаляет оповещения категории half_attack и колонки ударов в снимке на перерыве.
DELETE FROM live_alerts_sent WHERE category = 'half_attack';
ALTER TABLE live_alerts_sent DROP CONSTRAINT live_alerts_sent_category_check;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_category_check CHECK (category IN ('stats', 'half'));
ALTER TABLE live_ht_possession DROP COLUMN IF EXISTS away_shots_on_goal;
ALTER TABLE live_ht_possession DROP COLUMN IF EXISTS home_shots_on_goal;
