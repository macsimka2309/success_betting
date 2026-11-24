CREATE OR REPLACE VIEW fonbet_live_statistics_75_normalized AS
WITH scores AS (
 SELECT f.*,
  CASE WHEN ht_home IS NOT NULL AND goals_home >= ht_home THEN goals_home::integer-ht_home END AS goals_2h_home,
  CASE WHEN ht_away IS NOT NULL AND goals_away >= ht_away THEN goals_away::integer-ht_away END AS goals_2h_away
 FROM fonbet_live_match_statistics_75 f
)
SELECT scores.*,
 shots_on_target_2h_home::integer+goals_2h_home AS shots_including_goals_2h_home,
 shots_on_target_2h_away::integer+goals_2h_away AS shots_including_goals_2h_away,
 shots_on_target_2h_home::integer+goals_2h_home+shots_on_target_2h_away::integer+goals_2h_away AS shots_including_goals_2h_total
FROM scores;

DROP FUNCTION fonbet_second_half_goals(integer,integer);
DROP TABLE live_api_reminder_observations;
ALTER TABLE live_signal_tracking DROP COLUMN result_due_at, DROP COLUMN result_due_from_admission;
ALTER TABLE live_signal_reminders DROP COLUMN first_decision;
