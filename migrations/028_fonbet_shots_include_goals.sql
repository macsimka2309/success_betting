-- Preserve raw Fonbet shots; derive comparable values exactly once at read time.
ALTER TABLE fonbet_live_match_statistics_75
 ADD COLUMN ht_home SMALLINT CHECK (ht_home >= 0 AND ht_home <= goals_home),
 ADD COLUMN ht_away SMALLINT CHECK (ht_away >= 0 AND ht_away <= goals_away);
CREATE VIEW fonbet_live_statistics_75_normalized AS
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
CREATE OR REPLACE VIEW live_match_statistics_75_combined AS
WITH ranked_fonbet AS (
 SELECT f.*, row_number() OVER (
  PARTITION BY CASE WHEN mapping_status='confirmed' THEN 'api:'||fixture_id ELSE 'fonbet:'||fonbet_event_id END
  ORDER BY elapsed DESC, observed_at DESC, fonbet_event_id DESC) AS position
 FROM fonbet_live_statistics_75_normalized f
), best_fonbet AS (SELECT * FROM ranked_fonbet WHERE position=1),
normalized_fonbet AS (
 SELECT f.*,
  CASE WHEN home_away_swapped THEN away_name ELSE home_name END AS aligned_home,
  CASE WHEN home_away_swapped THEN home_name ELSE away_name END AS aligned_away,
  CASE WHEN home_away_swapped THEN goals_away ELSE goals_home END AS aligned_goals_home,
  CASE WHEN home_away_swapped THEN goals_home ELSE goals_away END AS aligned_goals_away,
  CASE WHEN home_away_swapped THEN shots_including_goals_2h_away ELSE shots_including_goals_2h_home END AS aligned_shots_home,
  CASE WHEN home_away_swapped THEN shots_including_goals_2h_home ELSE shots_including_goals_2h_away END AS aligned_shots_away
 FROM best_fonbet f
)
SELECT 'api:'||a.fixture_id AS canonical_match_id, a.fixture_id,
 f.fonbet_event_id, 'api_football'::text AS source, a.league_id,
 COALESCE(th.name,raw.payload->'teams'->'home'->>'name',f.aligned_home) AS home_name,
 COALESCE(ta.name,raw.payload->'teams'->'away'->>'name',f.aligned_away) AS away_name,
 a.elapsed,a.goals_home,a.goals_away,a.statistics,a.quality_status,a.observed_at,
 a.observation_id AS api_observation_id, f.observation_id AS fonbet_observation_id,
 CASE WHEN f.fonbet_event_id IS NOT NULL THEN 'confirmed' ELSE 'api_only' END AS mapping_status,
 f.elapsed AS fonbet_elapsed,f.observed_at AS fonbet_observed_at
FROM live_match_statistics_75 a
LEFT JOIN normalized_fonbet f ON f.mapping_status='confirmed' AND f.fixture_id=a.fixture_id
LEFT JOIN teams th ON th.team_id=a.home_team_id
LEFT JOIN teams ta ON ta.team_id=a.away_team_id
LEFT JOIN LATERAL (
 SELECT item AS payload FROM api_observations o
 CROSS JOIN LATERAL jsonb_array_elements(o.payload->'response') item
 WHERE o.observation_id=a.context_observation_id AND item->'fixture'->>'id'=a.fixture_id::text LIMIT 1
) raw ON true
UNION ALL
SELECT CASE WHEN f.mapping_status='confirmed' THEN 'api:'||f.fixture_id ELSE 'fonbet:'||f.fonbet_event_id END,
 f.fixture_id,f.fonbet_event_id,'fonbet',core.league_id,
 f.aligned_home,f.aligned_away,f.elapsed,f.aligned_goals_home,f.aligned_goals_away,
 jsonb_build_object(
  'home',jsonb_build_object('match',NULL,'1h',NULL,'2h',jsonb_build_object('shots_on_goal',f.aligned_shots_home)),
  'away',jsonb_build_object('match',NULL,'1h',NULL,'2h',jsonb_build_object('shots_on_goal',f.aligned_shots_away))),
 CASE WHEN f.aligned_shots_home IS NOT NULL AND f.aligned_shots_away IS NOT NULL THEN 'both_teams' ELSE 'partial' END,f.observed_at,NULL::bigint,f.observation_id,f.mapping_status,f.elapsed,f.observed_at
FROM normalized_fonbet f
LEFT JOIN fixtures core ON core.fixture_id=f.fixture_id
WHERE NOT EXISTS (SELECT 1 FROM live_match_statistics_75 a WHERE f.mapping_status='confirmed' AND a.fixture_id=f.fixture_id);
