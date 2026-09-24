-- Явный состав собираемых лиг (ФТ-9): сбор идёт только по лигам с is_tracked.
-- Каталог (шаг 1) знает 1 246 лиг, а в охвате проекта 782; без флага сбор
-- тратил бы запросы на все лиги каталога.
-- ADD COLUMN с постоянным DEFAULT не перезаписывает таблицу (PostgreSQL 11+),
-- таблица leagues небольшая — блокировок нет.

ALTER TABLE leagues ADD COLUMN is_tracked BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN leagues.is_tracked IS
    'Лига входит в ежедневный сбор (ФТ-9). Ставится переносом из parquet и вручную; каталог её не меняет.';

-- Для баз, где перенос уже выполнен: лиги с прежним кодом — это наши 782.
UPDATE leagues SET is_tracked = true WHERE legacy_code IS NOT NULL;

CREATE INDEX idx_leagues_tracked ON leagues (league_id) WHERE is_tracked;
