-- Additive: legacy alerts, outcomes, snapshots and canonical identity are untouched.
CREATE TABLE live_signal_config (
 singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
 tracking_enabled BOOLEAN NOT NULL DEFAULT false,
 reminders_enabled BOOLEAN NOT NULL DEFAULT false,
 activated_at TIMESTAMPTZ,
 version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
 CHECK (NOT (tracking_enabled OR reminders_enabled) OR activated_at IS NOT NULL)
);
INSERT INTO live_signal_config DEFAULT VALUES;
CREATE TABLE live_signal_tracking (
 signal_id INTEGER PRIMARY KEY,
 alert_match_id BIGINT NOT NULL REFERENCES live_alert_matches,
 goals_enabled BOOLEAN NOT NULL DEFAULT true,
 feature_version INTEGER NOT NULL CHECK (feature_version > 0),
 first_message_id BIGINT NOT NULL CHECK (first_message_id > 0),
 first_sent_at TIMESTAMPTZ NOT NULL,
 confirmed_at TIMESTAMPTZ NOT NULL,
 delivery_started_at TIMESTAMPTZ NOT NULL,
 time_precision TEXT NOT NULL CHECK (time_precision IN ('telegram_second','delivery_interval')),
 first_context JSONB NOT NULL,
 status TEXT NOT NULL DEFAULT 'waiting_end' CHECK (status IN
 ('waiting_end','preliminary','review_due','reviewed','incomplete_final','void')),
 regulation_end_confirmed_at TIMESTAMPTZ,
 end_context JSONB,
 primary_due_at TIMESTAMPTZ,
 review_due_at TIMESTAMPTZ,
 review_day SMALLINT NOT NULL DEFAULT 0 CHECK (review_day BETWEEN 0 AND 8),
 primary_attempts INTEGER NOT NULL DEFAULT 0 CHECK (primary_attempts >= 0),
 last_attempt_at TIMESTAMPTZ,
 next_attempt_at TIMESTAMPTZ,
 lease_until TIMESTAMPTZ,
 timeline_complete BOOLEAN NOT NULL DEFAULT false,
 order_unknown_count INTEGER NOT NULL DEFAULT 0 CHECK (order_unknown_count >= 0),
 incomplete_reason TEXT,
 last_error TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK (confirmed_at >= delivery_started_at)
);
CREATE INDEX live_signal_match_idx ON live_signal_tracking(alert_match_id, first_sent_at);
CREATE INDEX live_signal_due_idx ON live_signal_tracking(next_attempt_at)
 WHERE status NOT IN ('reviewed','incomplete_final','void');
CREATE TABLE live_signal_reminders (
 signal_id INTEGER PRIMARY KEY REFERENCES live_signal_tracking,
 state TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN
 ('waiting','sending','sent','retry','uncertain','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 lease_until TIMESTAMPTZ,
 telegram_message_id BIGINT CHECK (telegram_message_id > 0),
 sent_at TIMESTAMPTZ,
 reason TEXT,
 context JSONB,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK (state <> 'sent' OR (telegram_message_id IS NOT NULL AND sent_at IS NOT NULL))
);
CREATE INDEX live_signal_reminder_due_idx ON live_signal_reminders(next_attempt_at)
 WHERE state IN ('waiting','retry');
CREATE TABLE live_signal_goal_observations (
 observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 alert_match_id BIGINT NOT NULL REFERENCES live_alert_matches,
 source TEXT NOT NULL CHECK (source IN ('api_football','fonbet')),
 source_event_id TEXT NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL,
 receipt_reference TEXT,
 payload JSONB NOT NULL,
 payload_hash TEXT NOT NULL,
 complete BOOLEAN NOT NULL,
 incomplete_reason TEXT,
 UNIQUE (alert_match_id, source, source_event_id, observed_at, payload_hash)
);
CREATE INDEX live_signal_goal_observation_match_idx
 ON live_signal_goal_observations(alert_match_id, observed_at);
CREATE TABLE live_signal_goals (
 goal_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 alert_match_id BIGINT NOT NULL REFERENCES live_alert_matches,
 event_key TEXT NOT NULL,
 source TEXT NOT NULL CHECK (source IN ('api_football','fonbet')),
 source_goal_id TEXT,
 observation_id BIGINT NOT NULL REFERENCES live_signal_goal_observations,
 credited_side TEXT CHECK (credited_side IN ('home','away')),
 source_team_id TEXT,
 minute SMALLINT CHECK (minute >= 0),
 added_minute SMALLINT CHECK (added_minute >= 0),
 period TEXT NOT NULL CHECK (period IN ('regulation_1h','regulation_2h','extra_time','shootout','unknown')),
 score_after_home SMALLINT CHECK (score_after_home >= 0),
 score_after_away SMALLINT CHECK (score_after_away >= 0),
 active BOOLEAN NOT NULL DEFAULT true,
 revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
 match_quality TEXT NOT NULL,
 UNIQUE (alert_match_id, source, event_key)
);
-- Immutable source snapshots retain all revisions; each interpretation has its own boundary.
CREATE TABLE live_signal_goal_relations (
 signal_id INTEGER NOT NULL REFERENCES live_signal_tracking,
 goal_id BIGINT NOT NULL REFERENCES live_signal_goals,
 relation TEXT NOT NULL CHECK (relation IN ('before','after','order_unknown')),
 reason TEXT,
 PRIMARY KEY (signal_id, goal_id)
);
CREATE TABLE live_signal_check_attempts (
 attempt_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 signal_id INTEGER NOT NULL REFERENCES live_signal_tracking,
 attempted_at TIMESTAMPTZ NOT NULL,
 stage TEXT NOT NULL,
 reason TEXT,
 details JSONB NOT NULL DEFAULT '{}'
);
-- Public analytical projection: one selected source per canonical signal.
-- Source-level rows remain separately auditable and must not be summed together.
CREATE VIEW live_signal_goals_selected AS
 SELECT t.signal_id,t.alert_match_id,g.goal_id,g.credited_side,g.minute,g.added_minute,g.period,
        g.score_after_home,g.score_after_away,g.source,g.source_goal_id,g.observation_id,
        r.relation,r.reason AS order_reason,t.timeline_complete,t.status
 FROM live_signal_tracking t JOIN live_signal_goal_relations r USING(signal_id)
 JOIN live_signal_goals g USING(goal_id)
 WHERE t.goals_enabled AND t.status<>'void' AND g.active;
