-- Источник статистики оповещения half_shots: half — второй тайм (правило 1), match — за матч (правило 2).
-- Для остальных категорий и прежних строк NULL.
ALTER TABLE live_alerts_sent ADD COLUMN stats_source TEXT CHECK (stats_source IN ('half', 'match'));
COMMENT ON COLUMN live_alerts_sent.stats_source IS
    'half_shots: half — shots_on_goal и possession за 2-й тайм, match — за матч (запасной вариант).';
