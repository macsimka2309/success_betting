-- Проходимость оповещений (specs/онлайн-оповещения.md, «Учёт проходимости»): для каждого
-- оповещения после матча фиксируется, был ли гол после 75-й минуты. Денег нет.
-- outcome: pending — ждёт конца матча; hit — после 75-й минуты был хотя бы один гол;
-- miss — голов после 75-й минуты не было; void — матч отменён; no_data — события источника
-- неполны (число голов в событиях не сходится со счётом), оценке не подлежит.
-- Таблица маленькая, ADD COLUMN с постоянным DEFAULT блокировок не создаёт.

ALTER TABLE live_alerts_sent ADD COLUMN outcome TEXT NOT NULL DEFAULT 'pending'
    CHECK (outcome IN ('pending', 'hit', 'miss', 'void', 'no_data'));
ALTER TABLE live_alerts_sent ADD COLUMN goals_after_75 SMALLINT;
ALTER TABLE live_alerts_sent ADD COLUMN final_goals_home SMALLINT;
ALTER TABLE live_alerts_sent ADD COLUMN final_goals_away SMALLINT;
ALTER TABLE live_alerts_sent ADD COLUMN resolved_at TIMESTAMPTZ;

CREATE INDEX idx_live_alerts_outcome ON live_alerts_sent (outcome) WHERE outcome = 'pending';

CREATE VIEW alert_hit_rate AS
SELECT category,
       count(*)                                        AS alerts,
       count(*) FILTER (WHERE outcome IN ('hit', 'miss')) AS resolved,
       count(*) FILTER (WHERE outcome = 'hit')         AS hits,
       round(100.0 * count(*) FILTER (WHERE outcome = 'hit')
             / NULLIF(count(*) FILTER (WHERE outcome IN ('hit', 'miss')), 0), 1) AS hit_percent,
       count(*) FILTER (WHERE outcome = 'pending')     AS pending
FROM live_alerts_sent
GROUP BY category;

COMMENT ON VIEW alert_hit_rate IS
    'Проходимость оповещений по категориям: доля оповещений, после которых в матче был гол после 75-й минуты.';
