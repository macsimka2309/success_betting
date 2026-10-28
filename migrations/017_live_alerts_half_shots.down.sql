-- Откат 017: удаляет оповещения категории half_shots (в том числе как дополнительный признак).
DELETE FROM live_alerts_sent WHERE category = 'half_shots';
UPDATE live_alerts_sent SET also_categories = array_remove(also_categories, 'half_shots');
ALTER TABLE live_alerts_sent DROP CONSTRAINT live_alerts_sent_category_check;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_category_check
    CHECK (category IN ('stats', 'half', 'half_attack', 'favorite'));
