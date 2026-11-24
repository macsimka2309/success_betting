-- Read-only queue report, with independent reminder and Fonbet states.
SELECT t.signal_id,t.alert_match_id,t.first_sent_at,t.first_source,t.first_source_event_id,
       CASE WHEN t.first_source IS NULL THEN 'retired'
            WHEN t.first_source='api_football' OR NOT t.goals_enabled THEN 'reminder_only'
            ELSE 'fonbet_goals' END AS mode,
       r.state AS reminder_state,r.reason AS reminder_reason,r.first_decision AS reminder_first_decision,
       t.status,t.live_phase,t.last_live_slot,t.missed_live_slots,
       obs.observed_at AS last_live_timeline_at,
       obs.payload->'timeline'->>'elapsed' AS last_live_minute,
       obs.payload->'timeline'->>'added_minute' AS last_live_added_minute,
       valid.observed_at AS last_valid_timeline_at,
       t.regulation_end_confirmed_at,t.end_context,t.last_attempt_at,t.last_poll_at,t.next_attempt_at,
       t.result_due_at,t.result_due_from_admission,t.deferred_reason,
       CASE WHEN t.result_due_at IS NOT NULL AND t.status IN ('waiting_end','preliminary','review_due')
            THEN greatest(now()-t.result_due_at,interval '0 seconds') END AS result_wait,
       CASE WHEN t.first_source='fonbet' AND t.goals_enabled
            THEN CASE WHEN t.status IN ('reviewed','incomplete_final','void') THEN 0 ELSE 8-t.review_day END END AS remaining_daily_checks,
       COALESCE(t.first_source='fonbet' AND t.goals_enabled AND coalesce(t.result_due_at,t.next_attempt_at)<now()
                AND t.status IN ('waiting_end','preliminary','review_due'),false) AS overdue,
       COALESCE(t.first_source='fonbet' AND t.goals_enabled AND t.status<>'void'
          AND t.regulation_end_confirmed_at+interval '1 hour'<now() AND t.primary_attempts=0,false) AS initial_load_overdue,
       t.review_day,t.primary_attempts,t.timeline_complete,t.order_unknown_count,
       t.incomplete_reason,t.last_error
FROM live_signal_tracking t LEFT JOIN live_signal_reminders r USING(signal_id)
LEFT JOIN live_signal_goal_observations obs ON obs.observation_id=t.last_live_observation_id
LEFT JOIN LATERAL (
    SELECT o.observed_at FROM live_signal_goal_observations o
    WHERE (o.alert_match_id=t.alert_match_id OR o.observation_id=t.last_live_observation_id)
      AND o.source='fonbet' AND o.source_event_id=t.first_source_event_id
      AND (o.complete OR o.payload->'payload'->>'structure_valid'='true')
    ORDER BY o.observed_at DESC,o.observation_id DESC LIMIT 1
) valid ON t.first_source='fonbet'
ORDER BY t.first_sent_at DESC;
