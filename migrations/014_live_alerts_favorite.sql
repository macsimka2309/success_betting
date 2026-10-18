-- Категория favorite: фаворит дома против аутсайдера (specs/онлайн-оповещения.md).
ALTER TABLE live_alerts_sent DROP CONSTRAINT live_alerts_sent_category_check;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_category_check
    CHECK (category IN ('stats', 'half', 'half_attack', 'favorite'));
ALTER TABLE live_alerts_sent ALTER COLUMN possession DROP NOT NULL;
ALTER TABLE live_alerts_sent ADD COLUMN home_rank SMALLINT;
ALTER TABLE live_alerts_sent ADD COLUMN away_rank SMALLINT;
ALTER TABLE live_alerts_sent ADD COLUMN teams_count SMALLINT;
