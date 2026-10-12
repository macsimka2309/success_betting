-- Откат 004: удаляет таймы вместе с накопленными данными (их можно добрать
-- заново, но это ~143 тыс. запросов API). Выполнять только по явному решению.
-- fixture_statistics и итоговые значения не затрагиваются.

ALTER TABLE fixture_fetch_state DROP COLUMN IF EXISTS halves_attempts;
ALTER TABLE fixture_fetch_state DROP COLUMN IF EXISTS halves_fetched_at;
DROP TABLE IF EXISTS fixture_statistics_halves;
