ALTER TABLE live_signal_tracking
 ADD COLUMN pending_api_observation_id BIGINT REFERENCES live_signal_goal_observations,
 ADD COLUMN last_poll_at TIMESTAMPTZ,
 ADD COLUMN deferred_reason TEXT;
