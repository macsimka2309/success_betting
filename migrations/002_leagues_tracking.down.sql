-- Откат 002: убирает флаг охвата. Данные лиг не затрагиваются.
DROP INDEX IF EXISTS idx_leagues_tracked;
ALTER TABLE leagues DROP COLUMN IF EXISTS is_tracked;
