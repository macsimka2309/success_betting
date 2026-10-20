-- Откат 015: возвращает представление к учёту только основной категории.
CREATE OR REPLACE VIEW alert_hit_rate AS
SELECT category,
       count(*)                                        AS alerts,
       count(*) FILTER (WHERE outcome IN ('hit', 'miss')) AS resolved,
       count(*) FILTER (WHERE outcome = 'hit')         AS hits,
       round(100.0 * count(*) FILTER (WHERE outcome = 'hit')
             / NULLIF(count(*) FILTER (WHERE outcome IN ('hit', 'miss')), 0), 1) AS hit_percent,
       count(*) FILTER (WHERE outcome = 'pending')     AS pending
FROM live_alerts_sent
GROUP BY category;

ALTER TABLE live_alerts_sent DROP COLUMN also_categories;
