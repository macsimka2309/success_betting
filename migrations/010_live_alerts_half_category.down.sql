-- Откат 010: удаляет оповещения категории half (прежняя схема хранит одно оповещение на матч)
-- и таблицу владения на перерыве. Оповещения категории stats и ставки не затрагиваются.
DROP TABLE IF EXISTS live_ht_possession;
DELETE FROM live_alerts_sent WHERE category = 'half';
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS total_possession;
UPDATE live_alerts_sent SET shots_on_goal = 0 WHERE shots_on_goal IS NULL;
ALTER TABLE live_alerts_sent ALTER COLUMN shots_on_goal SET NOT NULL;
ALTER TABLE live_alerts_sent DROP CONSTRAINT IF EXISTS live_alerts_sent_fixture_category_key;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_pkey PRIMARY KEY (fixture_id);
ALTER TABLE live_alerts_sent DROP COLUMN IF EXISTS category;
