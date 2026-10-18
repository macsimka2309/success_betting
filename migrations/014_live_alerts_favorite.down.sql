DELETE FROM live_alerts_sent WHERE category = 'favorite';
ALTER TABLE live_alerts_sent DROP COLUMN teams_count;
ALTER TABLE live_alerts_sent DROP COLUMN away_rank;
ALTER TABLE live_alerts_sent DROP COLUMN home_rank;
ALTER TABLE live_alerts_sent ALTER COLUMN possession SET NOT NULL;
ALTER TABLE live_alerts_sent DROP CONSTRAINT live_alerts_sent_category_check;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_category_check
    CHECK (category IN ('stats', 'half', 'half_attack'));
