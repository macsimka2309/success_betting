CREATE TABLE live_signal_goal_pending (
 observation_id BIGINT NOT NULL REFERENCES live_signal_goal_observations,
 ordinal INTEGER NOT NULL,
 candidate_goal_ids JSONB NOT NULL,
 payload JSONB NOT NULL,
 reason TEXT NOT NULL,
 PRIMARY KEY(observation_id,ordinal)
);
CREATE TABLE live_signal_goal_versions (
 goal_id BIGINT NOT NULL REFERENCES live_signal_goals,
 observation_id BIGINT NOT NULL REFERENCES live_signal_goal_observations,
 payload JSONB NOT NULL,
 PRIMARY KEY(goal_id,observation_id)
);
