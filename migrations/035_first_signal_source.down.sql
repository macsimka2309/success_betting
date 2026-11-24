-- Do not replay retired work or let version-1 workers poll newer API signals.
UPDATE live_signal_config SET tracking_enabled=false,reminders_enabled=false,version=1;
ALTER TABLE live_signal_config ALTER COLUMN version SET DEFAULT 1;
CREATE OR REPLACE VIEW live_signal_goals_selected AS
 SELECT t.signal_id,t.alert_match_id,g.goal_id,g.credited_side,g.minute,g.added_minute,g.period,
        g.score_after_home,g.score_after_away,g.source,g.source_goal_id,g.observation_id,
        r.relation,r.reason AS order_reason,t.timeline_complete,t.status
 FROM live_signal_tracking t JOIN live_signal_goal_relations r USING(signal_id)
 JOIN live_signal_goals g USING(goal_id)
 WHERE t.goals_enabled AND t.status<>'void' AND g.active;

DROP INDEX live_signal_fonbet_due_idx;
ALTER TABLE live_signal_goals DROP COLUMN source_detail;
ALTER TABLE live_signal_tracking DROP CONSTRAINT live_signal_first_source_valid,
 DROP COLUMN first_source, DROP COLUMN first_source_event_id, DROP COLUMN live_phase,
 DROP COLUMN last_live_slot, DROP COLUMN last_live_observation_id, DROP COLUMN missed_live_slots;
