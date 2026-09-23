-- Откат 001_initial_schema.sql.
-- Порядок обратный созданию: сначала зависимые таблицы.
-- ВНИМАНИЕ: удаляет все данные схемы. На production применять только осознанно.

DROP TABLE IF EXISTS fixture_fetch_state;
DROP TABLE IF EXISTS collection_runs;
DROP TABLE IF EXISTS odds_values;
DROP TABLE IF EXISTS odds_snapshots;
DROP TABLE IF EXISTS injuries;
DROP TABLE IF EXISTS fixture_lineup_players;
DROP TABLE IF EXISTS fixture_lineups;
DROP TABLE IF EXISTS fixture_statistics;
DROP TABLE IF EXISTS fixture_events;
DROP TABLE IF EXISTS fixtures;
DROP TABLE IF EXISTS bet_types;
DROP TABLE IF EXISTS bookmakers;
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS teams;
DROP TABLE IF EXISTS league_seasons;
DROP TABLE IF EXISTS leagues;
