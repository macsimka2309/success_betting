-- Вторая категория оповещений: владение во втором тайме (specs/онлайн-оповещения.md).
-- 1) Оповещение теперь уникально по паре (матч, категория), а не по матчу: по одному матчу
--    могут сработать обе категории. Первичный ключ fixture_id заменяется уникальным
--    ограничением; на alert_id (уникален) ссылается bets, он не затрагивается.
-- 2) shots_on_goal допускает NULL (у категории «half» ударов нет), добавлено total_possession.
-- 3) live_ht_possession — владение команд на перерыве (итог на перерыве = первый тайм),
--    служит оценке второго тайма без лишних запросов к API; строки старше двух суток чистятся.
-- Таблица live_alerts_sent маленькая, блокировки кратковременные.

ALTER TABLE live_alerts_sent ADD COLUMN category TEXT NOT NULL DEFAULT 'stats'
    CHECK (category IN ('stats', 'half'));
ALTER TABLE live_alerts_sent DROP CONSTRAINT live_alerts_sent_pkey;
ALTER TABLE live_alerts_sent ALTER COLUMN fixture_id SET NOT NULL;
ALTER TABLE live_alerts_sent ADD CONSTRAINT live_alerts_sent_fixture_category_key UNIQUE (fixture_id, category);
ALTER TABLE live_alerts_sent ALTER COLUMN shots_on_goal DROP NOT NULL;
ALTER TABLE live_alerts_sent ADD COLUMN total_possession SMALLINT;

CREATE TABLE live_ht_possession (
    fixture_id      INTEGER     PRIMARY KEY,
    home_possession SMALLINT    NOT NULL,
    away_possession SMALLINT    NOT NULL,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE live_ht_possession IS
    'Владение на перерыве (первый тайм) идущих матчей. Рабочая память оповещений, не архив; чистится через двое суток.';
