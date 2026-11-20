ALTER TABLE live_signal_reminders
    ADD COLUMN first_decision JSONB
        CHECK (first_decision IS NULL OR (jsonb_typeof(first_decision->'triggered')='boolean') IS TRUE);

ALTER TABLE live_signal_tracking
    ADD COLUMN result_due_at TIMESTAMPTZ,
    ADD COLUMN result_due_from_admission BOOLEAN NOT NULL DEFAULT false;

-- Remember the exact live context used by the validated API statistics response,
-- even when a lower match minute leaves the analytical snapshot unchanged.
CREATE TABLE live_api_reminder_observations (
    observation_id BIGINT PRIMARY KEY REFERENCES api_observations,
    context_observation_id BIGINT NOT NULL REFERENCES api_observations,
    fixture_id INTEGER NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX live_api_reminder_fixture_idx
    ON live_api_reminder_observations(fixture_id, observed_at, observation_id);

-- Recover the original due time of tasks that are still open. Historical
-- terminal states and their audits are left intact.
WITH recovered AS (
    SELECT signal_id,
        CASE
            WHEN status='review_due' AND review_day>0
                THEN last_attempt_at+interval '24 hours'
            WHEN primary_attempts=0
                THEN coalesce(primary_due_at, regulation_end_confirmed_at)
            WHEN timeline_complete
                THEN coalesce(review_due_at,regulation_end_confirmed_at+interval '24 hours')
            WHEN last_attempt_at>=regulation_end_confirmed_at
                THEN CASE WHEN last_attempt_at>=regulation_end_confirmed_at+interval '1 hour'
                    THEN coalesce(review_due_at,regulation_end_confirmed_at+interval '24 hours')
                    ELSE least(last_attempt_at + CASE primary_attempts
                        WHEN 1 THEN interval '5 minutes' WHEN 2 THEN interval '15 minutes'
                        ELSE interval '30 minutes' END,
                        regulation_end_confirmed_at+interval '1 hour')
                    END
        END AS due
    FROM live_signal_tracking
    WHERE first_source='fonbet' AND goals_enabled AND regulation_end_confirmed_at IS NOT NULL
      AND status IN ('waiting_end','preliminary','review_due')
)
UPDATE live_signal_tracking t
SET result_due_at=coalesce(r.due,t.next_attempt_at),
    result_due_from_admission=(r.due IS NULL AND t.next_attempt_at IS NOT NULL)
FROM recovered r WHERE r.signal_id=t.signal_id;

-- Reuse the same goal adjustment in both the retained row and the archive.
CREATE FUNCTION fonbet_second_half_goals(total integer, half integer) RETURNS integer
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN CASE WHEN total>=half THEN total-half END;

CREATE OR REPLACE VIEW fonbet_live_statistics_75_normalized AS
WITH scores AS (
 SELECT f.*,
        fonbet_second_half_goals(goals_home,ht_home) AS goals_2h_home,
        fonbet_second_half_goals(goals_away,ht_away) AS goals_2h_away
 FROM fonbet_live_match_statistics_75 f
)
SELECT scores.*,
       shots_on_target_2h_home::integer+goals_2h_home AS shots_including_goals_2h_home,
       shots_on_target_2h_away::integer+goals_2h_away AS shots_including_goals_2h_away,
       shots_on_target_2h_home::integer+goals_2h_home+shots_on_target_2h_away::integer+goals_2h_away AS shots_including_goals_2h_total
FROM scores;
