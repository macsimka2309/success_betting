-- SET NOT NULL prevents rollback while manual bets still exist.
ALTER TABLE bets ALTER COLUMN fixture_id SET NOT NULL;
ALTER TABLE bets DROP CONSTRAINT bets_manual_match_check;
ALTER TABLE bets DROP COLUMN manual_home_name, DROP COLUMN manual_away_name;
