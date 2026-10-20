-- Карточка с несколькими признаками: основной признак в category, остальные в also_categories
-- (specs/онлайн-оповещения.md, раздел «Карточка с несколькими признаками»).
ALTER TABLE live_alerts_sent ADD COLUMN also_categories TEXT[] NOT NULL DEFAULT '{}';

CREATE OR REPLACE VIEW alert_hit_rate AS
WITH flat AS (
    SELECT c AS category, outcome
    FROM live_alerts_sent, LATERAL unnest(ARRAY[category] || also_categories) AS c
)
SELECT category,
       count(*)                                        AS alerts,
       count(*) FILTER (WHERE outcome IN ('hit', 'miss')) AS resolved,
       count(*) FILTER (WHERE outcome = 'hit')         AS hits,
       round(100.0 * count(*) FILTER (WHERE outcome = 'hit')
             / NULLIF(count(*) FILTER (WHERE outcome IN ('hit', 'miss')), 0), 1) AS hit_percent,
       count(*) FILTER (WHERE outcome = 'pending')     AS pending
FROM flat
GROUP BY category;
