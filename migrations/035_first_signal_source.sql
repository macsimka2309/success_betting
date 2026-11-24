-- Preserve the version-1 observations, but retire its pending work at cutover.
ALTER TABLE live_signal_tracking
 ADD COLUMN first_source TEXT CHECK (first_source IN ('api_football','fonbet')),
 ADD COLUMN first_source_event_id TEXT CHECK (first_source_event_id <> ''),
 ADD COLUMN live_phase TEXT CHECK (live_phase IN ('ordinary','late','unavailable')),
 ADD COLUMN last_live_slot TIMESTAMPTZ,
 ADD COLUMN last_live_observation_id BIGINT REFERENCES live_signal_goal_observations,
 ADD COLUMN missed_live_slots INTEGER NOT NULL DEFAULT 0 CHECK (missed_live_slots >= 0),
 ADD CONSTRAINT live_signal_first_source_valid CHECK (
   (first_source IS NULL) = (first_source_event_id IS NULL)
   AND (first_source IS DISTINCT FROM 'api_football' OR NOT goals_enabled)
 );
ALTER TABLE live_signal_goals ADD COLUMN source_detail TEXT;

CREATE INDEX live_signal_fonbet_due_idx ON live_signal_tracking(next_attempt_at,signal_id)
 WHERE first_source='fonbet' AND goals_enabled AND status IN ('waiting_end','preliminary','review_due');

UPDATE live_signal_reminders SET state='cancelled',reason='policy_changed',updated_at=now()
 WHERE state IN ('waiting','retry');
UPDATE live_signal_config SET version=2;
ALTER TABLE live_signal_config ALTER COLUMN version SET DEFAULT 2;

-- Keep new goals in provider orientation even if an alias is linked mid-match.
CREATE OR REPLACE VIEW live_signal_goals_selected AS
 SELECT t.signal_id,t.alert_match_id,g.goal_id,
        CASE WHEN f.home_away_swapped THEN CASE g.credited_side WHEN 'home' THEN 'away' WHEN 'away' THEN 'home' END
             ELSE g.credited_side END AS credited_side,
        g.minute,g.added_minute,g.period,
        CASE WHEN f.home_away_swapped THEN g.score_after_away ELSE g.score_after_home END AS score_after_home,
        CASE WHEN f.home_away_swapped THEN g.score_after_home ELSE g.score_after_away END AS score_after_away,
        g.source,g.source_goal_id,g.observation_id,r.relation,r.reason AS order_reason,t.timeline_complete,t.status
 FROM live_signal_tracking t JOIN live_signal_goal_relations r USING(signal_id)
 JOIN live_signal_goals g USING(goal_id)
 LEFT JOIN fonbet_live_match_statistics_75 f ON t.first_source='fonbet'
    AND f.fonbet_event_id=t.first_source_event_id AND f.mapping_status='confirmed'
 WHERE t.goals_enabled AND t.status<>'void' AND g.active;
