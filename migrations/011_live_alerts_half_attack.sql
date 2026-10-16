-- Третья категория оповещений: атака во втором тайме (specs/онлайн-оповещения.md):
-- у команды во втором тайме 4 и более ударов в створ и владение больше 55 %.
-- 1) category допускает 'half_attack'.
-- 2) Снимок на перерыве хранит ещё удары в створ обеих команд: удары складываются, поэтому
--    удары второго тайма = итог за матч минус снимок, без лишних запросов к API.
-- Таблицы маленькие, блокировки кратковременные.

ALTER TABLE live_alerts_sent DROP CONSTRAINT live_alerts_sent_category_check;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_category_check
    CHECK (category IN ('stats', 'half', 'half_attack'));

ALTER TABLE live_ht_possession ADD COLUMN home_shots_on_goal SMALLINT;
ALTER TABLE live_ht_possession ADD COLUMN away_shots_on_goal SMALLINT;
